# Tool Dispatcher Specification

**Status:** Draft v1.1
**Last updated:** 2026-05-08
**Owner:** _your name_

> **v1.1 changes:** Factory-vs-singleton registration clarified (§3.1).
> Cross-references to `tool.confirmation_*` events now in the bus catalog.

> *Throughout: paths shown use `~/.yourtool/` as a placeholder for the final config directory.*

---

## 1. Purpose

This document specifies the tool dispatcher: the component that registers tools, validates their input schemas, dispatches `ToolUseBlock` calls to the right tool implementation, classifies side effects, applies confirmation policy, and emits `tool.*` events.

The dispatcher is small but central. It sits between the agent loop (which receives tool_use blocks from the adapter) and the actual tool implementations (file ops, shell, MCP servers). Getting the contracts right means tools written by users and Phase 3 MCP servers can plug in without core changes.

This spec depends on:

- `canonical-message-format.md` for `ToolDefinition`, `ToolUseBlock`, `ToolResultBlock`, `SideEffects` enum.
- `event-bus-and-trace-catalog.md` for `tool.called`, `tool.completed`, `tool.failed`, `tool.input_invalid`.
- `provider-adapter-contract.md` for `ToolDefinition` shape sent to providers.

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Uniform dispatch.** All tools — file, shell, MCP, future plugins — go through one dispatcher with one event stream.
2. **Honest side-effect declaration.** Every tool declares what it does (read, write, execute, network); the dispatcher uses this to drive confirmation and routing.
3. **Strict input validation.** Tool inputs validate against JSON Schema before execution. Bad inputs fail loudly with structured errors.
4. **Workspace-scoped by default.** Tools that touch the filesystem cannot escape the session's workspace root unless explicitly granted.
5. **Cancellable.** Long-running tools can be interrupted cleanly when the user cancels a turn.
6. **Pluggable.** Adding a new tool is implementing one interface and registering. No core changes.

### 2.2 Non-goals

1. **Sandboxing.** v1 trusts tool implementations. Real sandboxing (containers, syscall filtering) is a Phase 4+ concern when third-party tools are common.
2. **MCP server lifecycle management.** v1 ships file and shell tools as built-ins. MCP integration ships in Phase 3 with its own lifecycle (start/stop/health) which extends but does not replace this contract.
3. **Tool versioning.** A tool's name uniquely identifies it; multiple versions of "the same" tool are different tool names. Versioning machinery is a marketplace concern (Phase 4+).
4. **Transactions across tools.** A tool either completes or fails; there's no rollback across multiple tool calls. The agent decides recovery.

---

## 3. The interface

### 3.1 Tool implementations

```python
class Tool(Protocol):
    """Implemented by every tool — built-in or MCP-wrapped."""

    definition: ToolDefinition       # name, description, input_schema, side_effects

    async def execute(
        self,
        input: dict,                  # validated against input_schema before this is called
        context: ToolContext,         # session-scoped data
    ) -> ToolOutput:
        """Run the tool. Return structured output. Raise ToolError subclasses
        on failure (see §6)."""

    async def cancel(self) -> bool:
        """Abort an in-flight execution. Returns True if cancelled cleanly,
        False if already completed or never started. Idempotent."""
```

Tools are stateful per-execution: a fresh `Tool` instance handles one execute() at a time. The dispatcher manages a pool; concurrent calls to the same tool name use separate instances. This avoids cross-call interference (e.g., a shell tool tracking its own subprocess).

**The registered thing is a factory, not an instance.** `register(tool_factory)` accepts a zero-argument callable that produces a fresh `Tool` when called. The dispatcher invokes the factory per dispatch. Implementers MUST NOT register a singleton `Tool` instance — concurrent dispatches would share state and corrupt each other. If a tool genuinely has no per-call state, the factory may return a long-lived object, but the contract assumes per-call instances and the test suite verifies factories produce distinct objects.

### 3.2 ToolContext

What the tool sees about the session:

```python
class ToolContext:
    session_id: str
    turn_id: str
    tool_use_id: str                 # canonical id for this call
    workspace_path: str              # absolute, ~ expanded; the session's root
    # Cancellation token — tools poll this periodically
    cancel_event: asyncio.Event
    # Logger scoped to this tool call
    logger: Logger
    # Limited environment access
    workspace_files: WorkspaceFileAPI  # see §5.1
```

Tools do not get general filesystem access via Python's `open()` etc.; they get a workspace-scoped API (§5.1) that enforces the workspace root. Tools that genuinely need broader access declare it via `side_effects: EXECUTE` or `side_effects: NETWORK` and use raw OS APIs at their own risk — but the dispatcher's confirmation policy (§5.2) applies.

### 3.3 ToolOutput

```python
class ToolOutput:
    content: list[ContentBlock]      # usually [TextBlock]; may include ImageBlock
    success: bool                     # True for normal completion; False for handled errors
    metadata: dict                    # tool-specific, opaque to the dispatcher
    # Side-effect record (populated for write/execute side effects)
    files_modified: list[str] | None
    command_executed: str | None
```

The dispatcher wraps `ToolOutput` into a canonical `ToolResultBlock` for the agent loop. Tools never construct canonical messages themselves.

### 3.4 The dispatcher

```python
class ToolDispatcher:
    def register(self, tool_factory: Callable[[], Tool]) -> None:
        """Register a tool. The factory creates fresh instances per call.
        Validates the tool's definition (input_schema is canonical-allowed,
        name is unique, side_effects declared). Raises ToolRegistrationError
        on validation failure."""

    def unregister(self, tool_name: str) -> None: ...

    def get_definitions_for_session(self, session: Session) -> list[ToolDefinition]:
        """Return tool definitions visible to this session. Filters out tools
        the session shouldn't see (e.g., memory tools are hidden from worker
        sessions per routing-engine §6.2.1)."""

    async def dispatch(
        self,
        tool_use: ToolUseBlock,
        session: Session,
    ) -> ToolResultBlock:
        """Validate input, apply confirmation policy, execute, emit events,
        return canonical result. The agent loop calls this for each tool_use
        in an assistant message."""

    async def cancel_session_tools(self, session_id: str) -> None:
        """Cancel all in-flight tool calls for a session. Called by session
        manager on turn cancellation."""
```

---

## 4. Dispatch flow

For each `ToolUseBlock` the agent loop receives from an adapter:

```
1. Look up tool by name in the registry.
   - Not found → return ToolResultBlock with is_error=true, error message.
                 Emit tool.failed with error_class=not_found.

2. Validate input against tool's input_schema (JSON Schema subset).
   - Invalid → return ToolResultBlock with is_error=true, validation errors.
                 Emit tool.input_invalid.

3. Check workspace scoping.
   - If tool requires_workspace and a path argument escapes the workspace root,
     reject (per §5.1). Emit tool.failed with error_class=permission_denied.

4. Apply confirmation policy.
   - If side_effects in {WRITE, EXECUTE, NETWORK} and confirmation required,
     prompt user via the streaming layer (see §5.2).
   - If user denies, return ToolResultBlock with is_error=true, "user denied".
     Emit tool.failed with error_class=user_denied.

5. Emit tool.called event.

6. Instantiate Tool from registered factory; create ToolContext.

7. Execute under timeout (configurable per tool, default 60s for non-execute,
   600s for execute and network).

8. On completion, emit tool.completed.
   On exception, emit tool.failed with classified error.
   On cancellation (cancel_event set), emit tool.failed with error_class=cancelled.

9. Construct canonical ToolResultBlock from ToolOutput.

10. Return to agent loop.
```

The flow is sequential within one tool call. Multiple tool_use blocks in one assistant message dispatch concurrently (one per `tool_use_id`), bounded by a per-session concurrency cap (default 4 — see §4.1).

### 4.1 Concurrent tool calls

If an assistant turn has multiple tool_use blocks (parallel tool calls), they dispatch concurrently up to the cap:

- Default concurrency cap: 4 tools at once per session.
- Cap is configurable in server config; not user-facing.
- Excess calls queue in arrival order.
- The agent loop waits for all tool results before sending the next LLM call (per the standard tool-loop flow).

Concurrency only applies *within one assistant message's tool calls*. Across messages, the agent loop is sequential (next LLM call doesn't start until all current tool results are in).

### 4.2 What concurrency does NOT do

It does not parallelize independent agent operations across turns, and it does not run "background" tools. v1 keeps the agent loop linear at the turn level; in-turn parallelism only happens if the model emits multiple tool_use blocks in one assistant message.

---

## 5. Side effects, workspace scoping, and confirmation

### 5.1 The workspace-scoped file API

Tools that touch the filesystem MUST go through `WorkspaceFileAPI` rather than raw OS calls. The API enforces workspace boundaries:

```python
class WorkspaceFileAPI:
    workspace_root: str                      # session's workspace, absolute

    def read(self, path: str) -> str: ...
    def read_bytes(self, path: str) -> bytes: ...
    def write(self, path: str, content: str) -> None: ...
    def write_bytes(self, path: str, content: bytes) -> None: ...
    def append(self, path: str, content: str) -> None: ...
    def exists(self, path: str) -> bool: ...
    def list(self, path: str) -> list[str]: ...
    def delete(self, path: str) -> None: ...
    def patch(self, path: str, old: str, new: str) -> None: ...   # str_replace style
```

All paths are resolved relative to `workspace_root`. Absolute paths are rejected unless they are within `workspace_root` after resolution. Symbolic links that point outside the workspace are rejected.

Path resolution order:

1. If path is absolute and starts with `workspace_root` (or a path equal to `workspace_root` after `realpath`), accept.
2. If path is relative, resolve against `workspace_root`.
3. Otherwise reject with `WorkspaceEscapeError`.

`..` segments are resolved during checking, not after. A path of `subdir/../../../etc/passwd` is rejected.

### 5.2 Confirmation policy

The dispatcher enforces user confirmation for tools whose declared side effects exceed a threshold. The threshold is configurable per session and per tool:

```yaml
# server config (~/.yourtool/server.yaml or similar)
tool_confirmation:
  default:
    NONE: auto         # never prompt
    READ: auto
    WRITE: prompt      # prompt by default
    EXECUTE: prompt
    NETWORK: prompt
  per_tool:
    git_status: auto         # this read-side EXECUTE is exempted
    npm_install: prompt
  trusted_workspaces:
    - ~/code/myproject       # within these, lower the bar
  trusted_workspace_overrides:
    EXECUTE: prompt          # but EXECUTE still prompts even in trusted
```

Confirmation modes:

- `auto` — execute without prompting. Only safe for `NONE` and `READ` by default.
- `prompt` — request user confirmation via the streaming layer. The dispatcher emits a confirmation-request event; the client renders a yes/no prompt; user response routes back via HTTP. Tool execution waits for the answer.
- `prompt_once` — prompt the first time; remember the user's choice for the session (Phase 2 refinement, deferred).
- `deny` — never execute. Useful for forbidden categories.

The default is conservative: `WRITE`, `EXECUTE`, and `NETWORK` prompt; `READ` and `NONE` are auto. The user can lower the bar in their own config (e.g., auto-approve writes within trusted workspaces).

### 5.3 The confirmation request flow

When a tool requires confirmation:

1. Dispatcher pauses the tool call (no execution started yet).
2. Emits `tool.confirmation_requested` event with: tool name, input summary, side effect class, projected workspace impact (e.g., "will modify `src/auth.ts`").
3. The streaming server surfaces this to all attached clients of the session.
4. Client renders a UI (TUI shows an inline prompt; dashboard shows a modal).
5. User's response (allow / deny / always-allow-this-tool) goes through HTTP `POST /sessions/{id}/turns/{turn_id}/confirmations/{request_id}`.
6. Dispatcher receives the response, emits `tool.confirmation_resolved` event, and either proceeds (allow) or aborts (deny).

If the user disconnects mid-confirmation: the dispatcher waits up to a configured timeout (default 5 minutes), then aborts the tool call with `error_class: confirmation_timeout`. The agent loop sees an error tool_result and decides what to do.

If multiple clients are attached and one approves while another denies: first-write-wins. The losing client sees the resolution event and updates its UI.

### 5.4 Side-effect classification — what each class means

| Class       | Meaning                                                                | Examples                                           |
|-------------|------------------------------------------------------------------------|----------------------------------------------------|
| `NONE`      | Pure computation; no I/O.                                              | Calculate, format, parse.                          |
| `READ`      | Reads filesystem or queries network without mutation.                  | `read_file`, `list_dir`, `web_fetch` (GET).        |
| `WRITE`     | Mutates filesystem within workspace.                                   | `write_file`, `delete_file`, `patch_file`.         |
| `EXECUTE`   | Runs arbitrary code or shell commands.                                 | `shell`, `npm_install`, `python_eval`.             |
| `NETWORK`   | Mutates external state via network (POST, etc.).                       | `web_post`, `slack_send`, `email_send`.            |

A tool MUST declare its highest side-effect class. A "read-then-write" tool is `WRITE`. A shell tool that the user happens to use only for reads is still `EXECUTE` because the underlying capability is execute. The classification is by capability, not typical usage.

### 5.5 Built-in tools

| Tool name      | Side effects | Description                                                                                                       |
|----------------|--------------|-------------------------------------------------------------------------------------------------------------------|
| `read_file`    | READ         | Read a file from the workspace. Optional `offset` (1-indexed start line) + `limit` (line count) for line slices.  |
| `write_file`   | WRITE        | Create or overwrite a file in the workspace.                                                                      |
| `patch_file`   | WRITE        | Replace a unique string in a file (str_replace style).                                                            |
| `list_dir`     | READ         | List directory contents (workspace-scoped).                                                                       |
| `grep_files`   | READ         | Regex search across the workspace; returns `path:line: snippet` hits.                                             |
| `shell`        | EXECUTE      | Run a shell command in the workspace directory.                                                                   |

Phase 1 shipped `read_file` / `write_file` / `patch_file` / `list_dir` / `shell`. The `read_file` slicing affordance (`offset` / `limit`) and the `grep_files` search tool landed 2026-05-22 to reduce over-reading on large workspaces — a tool call adds a full LLM round-trip carrying the result into context, so reading more than necessary is paid for twice (once when called, again on every subsequent turn until compaction). Memory tools (`memory_add`, `memory_replace`, `memory_consolidate`) ship in Phase 2; the `delegate` and `skill_save` planner tools ship in Phase 2.5 + Wave 17. MCP-wrapped tools remain out of scope for v1.

`grep_files` is in-process: it walks the workspace via `os.walk(followlinks=False)`, prunes common build/cache dirs (`.git`, `__pycache__`, `.venv`, `venv`, `node_modules`, `dist`, `build`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.tox`, `site`, `.next`, `.cache`), routes each file read through `WorkspaceFileAPI.read()` (so symlinks pointing outside the workspace are rejected by construction), skips binary files (`UnicodeDecodeError`) and files larger than 5 MB, caps results at `max_results` (default 50, hard ceiling 200), and trims any snippet longer than 300 chars. The agent's prior options for "find references" — shelling out to `grep -rn` (a full tool round-trip whose output also lives in context) or reading whole files until the reference was found — are both materially more expensive in tokens.

---

## 6. Errors

### 6.1 Tool error classes

```python
class ToolErrorClass(StrEnum):
    NOT_FOUND          = "not_found"           # tool name not registered
    VALIDATION_ERROR   = "validation_error"    # input failed schema
    PERMISSION_DENIED  = "permission_denied"   # workspace escape, etc.
    USER_DENIED        = "user_denied"         # confirmation denied
    TIMEOUT            = "timeout"             # exceeded configured limit
    EXECUTION_ERROR    = "execution_error"     # tool itself raised
    CANCELLED          = "cancelled"           # cancel_event was set
    CONFIRMATION_TIMEOUT = "confirmation_timeout"  # user didn't respond
```

These match the `error_class` values in event-bus §6.4 `tool.failed`.

### 6.2 Tool exception hierarchy

```python
class ToolError(Exception):
    error_class: ToolErrorClass
    message: str
    tool_use_id: str
    is_user_visible: bool   # whether the message should surface in TUI

class ToolNotFound(ToolError): pass
class ToolValidationError(ToolError):
    validation_errors: list[str]
class ToolPermissionDenied(ToolError): pass
class ToolUserDenied(ToolError): pass
class ToolTimeout(ToolError): pass
class ToolExecutionError(ToolError):
    underlying: Exception | None
class ToolCancelled(ToolError): pass
class ConfirmationTimeout(ToolError): pass
```

The dispatcher catches all `ToolError` subclasses and converts them to `ToolResultBlock` with `is_error: true` and the error message. Unhandled non-`ToolError` exceptions from a tool are caught, wrapped as `ToolExecutionError` with `is_user_visible: false`, and logged at ERROR level with the underlying traceback.

### 6.3 What the agent sees

The agent always gets a `ToolResultBlock`, never a raw exception. On error, the block has `is_error: true` and human-readable error text. The agent can decide to retry, take over, or surface to the user — same as for any tool result.

This means tool errors are part of the conversational substrate, not exceptional control flow. A tool that "fails" semantically (`read_file` on a missing path) returns a result block with `is_error: true`. A tool that fails to even attempt execution (timeout, validation error, permission denied) likewise returns a result block. Only adapter-level errors (provider down, network) raise out of the dispatcher.

---

## 7. Input schema validation

### 7.1 The JSON Schema subset

Tool input schemas are validated using the JSON Schema subset defined in canonical-format §5.4:

- **Allowed:** `string`, `number`, `integer`, `boolean`, `null`, `object`, `array`, `enum`, `required`, `properties`, `items`, `description`, basic `format` annotations.
- **Disallowed:** `$ref`, `oneOf`, `anyOf`, `allOf`, `not`, `if`/`then`/`else`, `patternProperties`, `additionalProperties: <schema>` (boolean is OK).

Adapters reject tool definitions with disallowed constructs at registration time. This prevents user-written tools from using JSON Schema features that some providers don't support.

### 7.2 Validation timing

Two validation points:

1. **At registration:** the dispatcher validates the schema's *structure* (only allowed JSON Schema constructs). Failures raise `ToolRegistrationError`.
2. **At dispatch:** the dispatcher validates each `ToolUseBlock.input` against the schema. Failures emit `tool.input_invalid` and return an error result block.

Adapter-side parsing errors (the model produced syntactically invalid JSON) are caught upstream — by the adapter when it parses the streamed tool input, or by the canonical layer if the input is malformed somehow. The dispatcher assumes it receives a parsed dict; schema validation happens on that dict.

### 7.3 Validation library

The implementation uses `jsonschema` (Python's standard JSON Schema library) with `Draft7Validator` and a custom subset checker that rejects disallowed constructs.

---

## 8. Cancellation

When the session manager cancels a turn (per `streaming-protocol.md` §6 and `routing-engine.md` §3.4):

1. Session manager calls `dispatcher.cancel_session_tools(session_id)`.
2. Dispatcher iterates in-flight tool calls for the session.
3. For each, sets `context.cancel_event`. Tools that poll the event abort and raise `ToolCancelled`.
4. For each, calls `tool.cancel()`. Tool-specific cleanup (kill subprocess, close file handles).
5. Each cancelled tool emits `tool.failed` with `error_class: cancelled` after cleanup.

Tool-specific cancellation behavior:

- **File operations** (`read_file`, `write_file`, etc.) — typically complete in <100ms; cancellation is ignored unless the operation is somehow stuck (network filesystem). They check `cancel_event` once before starting and finish if started.
- **Shell** — sends SIGTERM to the subprocess; SIGKILL after 5 seconds if still running. Returns whatever output was captured before termination.
- **Long-running custom tools** — must poll `cancel_event` periodically (every loop iteration, every chunk read). Tools that don't poll are forcibly cleaned up on context exit but may leak resources or partial state.

The dispatcher does not wait indefinitely for tools to honor cancellation. After 30 seconds, the dispatcher abandons the tool (logs at WARN; the tool's cleanup may still complete eventually) and proceeds with `tool.failed`.

---

## 9. Worked examples

### 9.1 Happy path

```
Agent emits ToolUseBlock(id="tu_01HZ_a", name="read_file", input={"path": "README.md"})

Dispatcher:
  1. Look up "read_file" → found.
  2. Validate input against schema → OK.
  3. Workspace scope check: "README.md" relative → resolves to workspace_root/README.md → OK.
  4. Confirmation: side_effects=READ → auto, no prompt.
  5. Emit tool.called {tool_name: "read_file", tool_use_id: "tu_01HZ_a", side_effects: "read"}.
  6. Instantiate ReadFileTool, build ToolContext.
  7. Execute. Returns ToolOutput(content=[TextBlock("# Project Foo\n...")], success=True).
  8. Emit tool.completed.
  9. Construct ToolResultBlock(tool_use_id="tu_01HZ_a", content=[...], is_error=False).
  10. Return to agent loop.
```

### 9.2 Workspace escape rejection

```
Agent emits ToolUseBlock(id="tu_01HZ_b", name="read_file", input={"path": "../../etc/passwd"})

Dispatcher:
  1. Look up "read_file" → found.
  2. Validate input → OK.
  3. Workspace scope check: resolves to /etc/passwd which is outside workspace_root.
     Raises WorkspaceEscapeError.
  4. Catch → ToolPermissionDenied(message="Path '../../etc/passwd' escapes workspace boundary").
  5. Emit tool.failed {error_class: "permission_denied", message: "..."}.
  6. Construct ToolResultBlock with is_error=True, content=[TextBlock("Path escapes workspace boundary")].
  7. Return to agent loop. Agent sees the error result and decides what to do.
```

### 9.3 User-denied write

```
Agent emits ToolUseBlock(id="tu_01HZ_c", name="write_file",
                          input={"path": "src/auth.ts", "content": "..."})

Dispatcher:
  1. Look up "write_file" → found.
  2. Validate → OK.
  3. Workspace scope → OK.
  4. Confirmation policy: WRITE → prompt.
  5. Emit tool.confirmation_requested {tool_name, input_summary, projected_modifications: ["src/auth.ts"]}.
  6. Wait. User sees prompt in TUI.
  7. User responds via HTTP: deny.
  8. Emit tool.confirmation_resolved {decision: "deny"}.
  9. Raise ToolUserDenied.
  10. Emit tool.failed {error_class: "user_denied"}.
  11. Construct ToolResultBlock with is_error=True, content=[TextBlock("User denied this operation.")].
  12. Return.
```

### 9.4 Cancellation mid-shell

```
Agent emits ToolUseBlock(id="tu_01HZ_d", name="shell", input={"command": "npm install"}).

Dispatcher: confirmed (or auto in trusted workspace), starts subprocess.

[2 minutes pass; npm install still running]

User cancels turn via WebSocket.
Session manager: cancel_session_tools(session_id).
Dispatcher: sets context.cancel_event; calls tool.cancel().
ShellTool.cancel(): SIGTERM to subprocess.

[3 seconds pass; subprocess still running]

ShellTool.cancel(): SIGKILL.
Subprocess terminates. ShellTool's execute() returns ToolOutput with partial captured output and success=False.

Dispatcher emits tool.failed {error_class: "cancelled", partial_output: "..."}.
Construct ToolResultBlock with is_error=True.

[turn.cancelled flow continues per streaming-protocol §6.2]
```

### 9.5 Concurrent dispatch

```
Agent emits an assistant message with three tool_use blocks:
  - tu_01HZ_e: read_file("a.txt")
  - tu_01HZ_f: read_file("b.txt")
  - tu_01HZ_g: read_file("c.txt")

Dispatcher: concurrency cap is 4; all three dispatch immediately.

Each emits tool.called, executes, emits tool.completed.

Agent loop waits until all three results are in, then sends the next LLM call
with the three tool_result blocks in a TOOL message (per canonical format).
```

---

## 10. Testing strategy

### 10.1 Required tests

1. **Tool registration validation.** Register a tool with disallowed schema constructs; verify rejection.
2. **Duplicate name rejection.** Register two tools with the same name; verify rejection.
3. **Input validation.** For each side-effect class, dispatch a call with malformed input; verify `tool.input_invalid` and error result.
4. **Workspace boundary enforcement.** Dispatch tools with paths that escape workspace via `..`, absolute paths outside, symlinks; verify rejection.
5. **Confirmation prompt and user-allow.** Configure WRITE → prompt; dispatch a write; verify confirmation event emitted, simulate allow, verify execution proceeds.
6. **Confirmation prompt and user-deny.** Same setup; simulate deny; verify error result with `user_denied`.
7. **Confirmation timeout.** Configure 1-second confirmation timeout; dispatch and don't respond; verify abort with `confirmation_timeout`.
8. **Concurrent tool calls.** Dispatch 6 tools (cap=4); verify 4 run concurrently, 2 queue, all complete in order.
9. **Cancellation honors cancel_event.** Dispatch a long-running tool; cancel mid-execution; verify tool aborts and emits `cancelled`.
10. **Shell cancellation via SIGTERM/SIGKILL.** Long-running shell command; cancel; verify SIGTERM, then SIGKILL after 5s if still running.
11. **Tool exception wrapping.** Tool implementation raises an unhandled exception; verify wrapped as `ToolExecutionError` with traceback in logs but redacted message in result.
12. **Workspace-scoped file API enforcement.** Even tools that bypass the API can't access files outside (verify via test-only tool that tries `open()` directly — this should be permitted but logged for debugging; sandboxing is non-goal).
13. **Trusted workspace lowers the bar.** Configure workspace as trusted; verify WRITE auto-runs but EXECUTE still prompts (per the override config example).
14. **Side-effect classification matches event payload.** For each built-in tool, verify the `tool.called.side_effects` field matches the tool's declared class.

---

## 11. Open questions

1. **MCP server lifecycle.** Phase 3 adds MCP. Specifically: how does the dispatcher learn an MCP server's tool definitions? How are MCP server crashes handled — does the dispatcher restart, surface as failure, do nothing? Deferred to MCP integration spec.
2. **`prompt_once` confirmation mode.** "Approve this tool for the rest of the session." Useful but adds session state; deferred to Phase 2.
3. **Tool versioning.** When a tool's behavior changes meaningfully, sessions persisted with the old behavior may not replay correctly. v1 ignores this; deferred to marketplace/Phase 4.
4. **Resource limits per tool.** Memory and CPU caps for shell and EXECUTE tools. v1 has timeout only; CPU/mem caps deferred.
5. **Per-tool concurrency caps.** v1 has a session-level cap of 4. Per-tool caps (e.g., "only one shell at a time") would prevent some tools from being trampled by parallel calls. Deferred.
6. **Streaming tool output.** v1 tools return complete output. A tool like `shell` could stream stdout/stderr as it produces; the dispatcher would emit `tool.output_delta` events. Phase 2 — useful for long-running commands.
7. **Side-effect classification for MCP tools.** MCP servers declare their own side effects, but third-party MCP servers may lie. Spec doesn't trust MCP declarations blindly; user-controlled override list deferred.

---

## 12. Decision log

| Date       | Decision                                                              | Rationale                                                                                  |
|------------|-----------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| 2026-05-08 | All tools go through one dispatcher, one event stream                 | Uniform tracing, uniform confirmation policy, uniform cancellation.                        |
| 2026-05-08 | Workspace-scoped file API by default; raw OS access is opt-in via EXECUTE | Most tools don't need to escape; explicit declaration for those that do.               |
| 2026-05-08 | Confirmation policy is per-class with per-tool overrides              | Sensible defaults (WRITE/EXECUTE/NETWORK prompt); user can lower the bar selectively.      |
| 2026-05-08 | Tool errors return as result blocks, not exceptions                   | Errors are conversational; agent decides recovery; only adapter errors raise.              |
| 2026-05-08 | Closed `ToolErrorClass` enum                                          | Consistent classification for analytics and dashboard.                                     |
| 2026-05-08 | JSON Schema subset matches canonical-format §5.4                      | Adapters and dispatcher share one constraint; tools authored once work everywhere.         |
| 2026-05-08 | Concurrency cap is session-level, default 4                           | Bounds blast radius of parallel tool calls; per-tool caps deferred.                        |
| 2026-05-08 | Side-effect classification is by capability, not typical usage        | Honesty about what a tool *can* do; user judges by worst case.                             |
| 2026-05-08 | Fresh Tool instance per call                                          | No cross-call state; concurrent calls don't interfere; cancellation is per-instance.       |
| 2026-05-08 | Confirmation request is via streaming + HTTP, not WebSocket return    | Matches streaming protocol's separation: WebSocket is one-way events; HTTP for actions.    |

---

## 13. References

- `canonical-message-format.md` — `ToolDefinition`, `ToolUseBlock`, `ToolResultBlock`, `SideEffects` enum, JSON Schema subset (§5.4).
- `event-bus-and-trace-catalog.md` — `tool.called`, `tool.completed`, `tool.failed`, `tool.input_invalid`, `tool.confirmation_requested`, `tool.confirmation_resolved` (the last two added in event-bus v3 alongside this spec's confirmation flow).
- `provider-adapter-contract.md` — how `ToolDefinition` flows from the dispatcher's registry to the adapter's wire serialization.
- `streaming-protocol.md` — how `tool.confirmation_requested` flows to clients; how cancellation propagates back to the dispatcher.
- `routing-engine.md` — worker session tool restrictions (§6.2.1); the dispatcher hides memory tools from worker sessions.
- `server-api.md` (planned) — the HTTP confirmation-response endpoint.
