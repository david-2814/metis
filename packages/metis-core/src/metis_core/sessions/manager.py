"""SessionManager: the agent turn loop.

Ties together routing → adapter → tool dispatcher → message store, with
event emission at every meaningful boundary. The model chosen at turn start
owns the entire turn including all tool cycles (routing-engine.md §3.2).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ulid import ULID

from metis_core.adapters.errors import AdapterError, CancelledError
from metis_core.adapters.protocol import (
    CanonicalRequest,
    StopReason,
    TokenUsage,
)
from metis_core.adapters.streaming import (
    MessageComplete,
    StreamingEvent,
)
from metis_core.adapters.tool_id_map import ToolIdMap
from metis_core.canonical.content import (
    ContentBlock,
    ImageBlock,
    TextBlock,
    ToolUseBlock,
)
from metis_core.canonical.ids import new_message_id
from metis_core.canonical.messages import (
    Message,
    MessageMetadata,
    MessageStatus,
    Role,
    RoutingDecisionRecord,
    RoutingMode,
    Usage,
)
from metis_core.events.bus import EventBus
from metis_core.events.envelope import Actor
from metis_core.events.payloads import (
    DelegateStarted,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    MemoryEviction,
    MemoryUpdated,
    SkillLoaded,
    TurnCancelled,
    TurnCompleted,
    TurnStarted,
    make_event,
)
from metis_core.memory.store import MemoryStore
from metis_core.pricing import PriceTable
from metis_core.routing import (
    ModelRegistry,
    OverrideParseResult,
    RoutingDecision,
    RoutingEngine,
    TurnContext,
    parse_per_message_override,
)
from metis_core.routing.engine import RoutingError
from metis_core.sessions.store import Session, SessionStore
from metis_core.skills.activation import SkillActivationRegistry
from metis_core.tools.dispatcher import ToolDispatcher
from metis_core.workers.protocol import (
    DelegateOutcome,
    DelegateRequest,
    DelegateUsageSummary,
)

logger = logging.getLogger(__name__)


DEFAULT_SYSTEM_PROMPT = (
    "You are Metis, an AI assistant operating in a developer's workspace. "
    "Use the available tools to read and modify files, run shell commands, "
    "and answer questions about the workspace. Be concise."
)

# Minimum-cacheable-prefix rule (context-assembler.md §5.1). The Anthropic
# provider silently drops `cache_control` markers when the cached prefix
# tokenizes below the per-model floor. The Anthropic docs cite 2048 tokens
# for haiku and 1024 for sonnet/opus, but a live haiku-4-5 probe found the
# effective floor sits higher — a 3320-actual-token prefix produced
# `cache_creation = 0`; a 4957-actual-token prefix worked. We target ~4500
# heuristic tokens (≈5670 actual at typical English-prose tokenization)
# so the cache fires on every model in the family with margin.
MIN_CACHEABLE_PREFIX_TOKENS = 4500
MAX_CACHEABLE_PREFIX_TOKENS = 5500

# Substantive, byte-stable operating-context block appended to the stable
# system prompt when the natural prefix tokenizes below the cache floor.
# Per context-assembler.md §5.1, this MUST be a module-level constant
# (no per-call I/O, timestamps, or run-specific data) so the cached prefix
# is byte-identical turn-to-turn within a session. Length target: ~22 KB
# (~5.5K heuristic tokens) so the block alone clears the effective haiku
# floor even with zero tools and a minimal base persona.
_OPERATING_CONTEXT_PADDING = """## Operating context

The sections below describe how Metis operates on a developer's workspace.
They are durable instructions, not transient state: the contents do not
change turn-to-turn within a session, so the provider can cache them.

### Workspace boundary

Tools that touch the filesystem operate inside the session's workspace
root. Paths are resolved relative to that root; absolute paths must lie
within the root. Path-escape attempts (".." segments, symlinks pointing
outside) are refused by the workspace API. When uncertain whether a
path lies inside the workspace, prefer `list_dir` to discover its
location before reading or writing. The workspace root is the only
filesystem-shaped boundary the agent should treat as authoritative;
anything outside that root is not the agent's concern unless the user
explicitly opts in through a tool that accepts an absolute path.

### Tool etiquette

- `read_file` returns the full text of a file. When a file is large,
  the model should reason about the returned text in memory rather
  than re-reading the same file on each step. Re-reading is wasteful
  and produces no new signal beyond the first read.
- `write_file` overwrites a file in place. Prefer it for new files
  or for whole-file rewrites where the final shape is known. Avoid
  using it to apply a small edit to a large file — the cost is
  proportional to the file size, not the diff size.
- `patch_file` replaces a single unique string with a single new
  string. It is the right tool for surgical edits: it fails loudly
  if the `old` string is missing or matches more than once, which
  protects against silent corruption of unrelated call sites. Pass
  enough surrounding context in `old` to make the match unique.
- `list_dir` lists a directory's contents. Prefer it for discovery
  before deciding which files to read. Avoid recursive listings of
  large trees; ask for a specific subtree, and chain follow-up
  listings only when the first reveals an interesting subdirectory.
- `shell` runs a command in the workspace. Commands should be safe
  to retry; commands that mutate global state outside the workspace
  (package managers, git remotes, deploy scripts) require human
  confirmation through the tool-confirmation flow. Always pass
  absolute paths or set cwd explicitly — never assume the working
  directory is where the previous command left it.
- Tool calls in a single turn dispatch in parallel. When two tool
  calls don't depend on each other, batch them in the same
  assistant message rather than serializing across two turns.

### Editing discipline

- Preserve existing formatting, naming conventions, and import order.
  A diff that gratuitously reformats unrelated lines is harder to
  review and obscures the actual change. Match what's already there.
- When changing the signature of a function or method, find every
  caller and update the call site in the same turn. Half-applied
  refactors are worse than no refactor at all — they leave the
  codebase in a state where neither old nor new contract holds.
- Comments explain why, not what. A comment that restates the
  identifier name is camouflage; remove it. A comment that records
  a constraint (a workaround for a specific bug, an invariant that
  callers depend on, a subtle reason the obvious approach was
  rejected) earns its place.
- New files should be small. Files that grow past a few hundred
  lines are usually two files pretending to be one; split them on
  a clean boundary when you can identify one. Don't artificially
  split a tightly coupled module just to hit a line-count target.
- Removing code is usually safer than refactoring it. If a path
  is unused, delete it. If it's used by one caller, inline it.
  Keep speculative generality out of the codebase.

### Naming

- Name things after what they *are*, not how they're used. Usage
  drifts faster than identity; a name tied to usage will mislead
  the next reader within months.
- Function names should describe the action and its target, in that
  order: `read_file`, `compute_cost`, `mark_failure`. Avoid generic
  verbs like `handle` or `process` that hide what actually happens.
  A function named `process_data` could do anything.
- Variable names should make the type and intent obvious without
  forcing the reader to scroll. Single-letter names are fine for
  tight scopes (loops, comprehensions) and almost never appropriate
  for parameters that outlive a few lines.
- Boolean names should read as predicates: `is_ready`, `has_errors`,
  `should_retry`. Avoid double negatives — `is_not_invalid` is
  worse than `is_valid` even when the semantics are identical.
- Type names should be nouns; function names verbs; predicates the
  third-person singular form. Consistency on these conventions
  pays off more than the local choice does.

### Errors and edge cases

- Validate inputs at the system boundary (user input, network
  responses, file contents). Inside a module, trust the contracts
  the rest of the module has already enforced; redundant validation
  is noise that obscures the load-bearing checks.
- Error messages seen by humans should name the action that
  triggered them, not the layer that detected the problem.
  "Failed to write `report.md`: disk full" is useful; "OSError 28"
  is not. The reader doesn't care which layer noticed the disk
  was full; they care what the agent was trying to do.
- Error messages seen by machines should keep their shape stable
  across releases. Downstream tooling pattern-matches on the
  message; a cosmetic edit can break a runbook. Treat user-facing
  and machine-facing strings as separate contracts.
- When a tool fails, the failure surfaces to the agent as a
  `tool_result` with `is_error=True`. The next assistant message
  should acknowledge the failure and decide whether to retry,
  adapt, or surface the failure to the user. Silent retries on
  errors that aren't transient are a bug.
- Catch the narrowest exception that captures the failure mode you
  intend to handle. Catching `Exception` is almost always wrong.

### Style

- Be terse. Lead with the answer. Code blocks only when they are
  load-bearing. Most explanations the agent might write don't need
  examples — the words alone suffice.
- When citing a file, use the form `path/to/file.py:LINE` so the
  reader can navigate directly. When citing a range, use
  `path/to/file.py:LINE-LINE`.
- When making a claim the user can verify, link to the source —
  a file path, a doc section, a commit. Unsourced claims are
  effectively guesses.
- When you don't know, say so explicitly and name what would
  resolve the uncertainty. "I'd need to read `config.yaml` to
  answer that" beats a confident guess every time.

### Concurrency and shared state

- Concurrency bugs hide in shared mutable state. Prefer message
  passing or immutable structures over shared locks when possible.
  Locks are a last resort, not a default.
- When a lock is necessary, scope it tightly. A lock held across
  an `await` is a lock held for an unbounded time and a
  prerequisite for deadlock.
- Tool dispatches inside a turn run in parallel. Each tool gets a
  fresh instance, so per-call state inside a tool is safe; shared
  state (a class-level cache, a module global) must be either
  read-only or protected explicitly.
- Async code that blocks the event loop on synchronous I/O is a
  bug masquerading as async. If a call can block, push it into a
  thread pool or call the async variant.

### Performance

- Performance work without a measurement is decoration. Profile
  before, profile after. Without a measurement, an "optimization"
  is just a code change that risks correctness for no proven gain.
- Premature optimization is reading three files to avoid the
  cost of reading one. The cost of a wrong answer is usually
  larger than the cost of an extra read.
- A function that grows past one screen is hiding bugs in its
  middle. Split it on the cleanest available boundary; even a
  mechanical split makes the file easier to reason about.
- Cache reads, but never assume the cache is fresh. The cost of a
  stale cache hit is always paid by the user, not the system.

### Communication norms

- A clarifying question early is cheaper than a wrong
  implementation discovered late. "I'm about to assume X — is
  that right?" beats "I assumed X and the diff is wrong".
- A clarifying question late is more expensive than continuing
  with the existing plan. Once the work is most of the way done,
  finish it; revisit the assumption in review.
- When something is unclear, write a one-line question, not a
  five-paragraph guess. The user can answer one line in seconds.
- Status updates should report what changed, what failed, and
  what's next. Bullet lists beat paragraphs when the reader is
  triaging.

### Tests

- Tests that exercise the real database catch migration bugs that
  mocked tests silently approve. Use mocks only when the real
  dependency is impractical (network, time, randomness); even
  then, prefer fakes over mocks when the contract is small.
- A failing test names what broke. "test_user_creation_failed" is
  not a good name; "test_user_creation_rejects_duplicate_email"
  is. The first leaves the reader guessing; the second points to
  the contract.
- Tests should be deterministic. Flaky tests are worse than
  missing tests because they teach the team to ignore failures.
- A test that depends on the order of dict iteration is a test
  waiting to fail in a later Python release. Sort explicitly
  when sequence matters.
- Coverage targets are a floor, not a goal. A test that exercises
  a line without asserting anything about its behavior is worse
  than no test at all — it manufactures confidence without earning
  it.

### Refactoring

- Before refactoring, prove the test suite passes. After
  refactoring, prove it still passes with the same set of tests.
  A green-to-green refactor is the safe one; a refactor that
  also changes tests is a behavior change disguised as a refactor.
- A bug fix does not need surrounding cleanup. Mixing the two
  makes the fix harder to review and harder to back out if the
  fix turns out to be wrong.
- Three similar lines is not a problem; an abstraction with two
  call sites is a problem in waiting. Wait for the third use
  before extracting.
- A refactor that improves the diff of one upcoming change but
  doesn't improve the codebase in isolation is overhead. Wait
  until you have two changes that both benefit.

### Feature flags

- Feature flags decay. Remove them within one release of full
  rollout (the flag is doing nothing) or full retirement (the
  flag is preserving dead code). A long-lived flag is a fork in
  the code base that the team forgets is there.
- A flag with no owner and no removal date is camouflage for
  abandonment. Either assign it or remove it.
- Flag-gated branches need their own test coverage; otherwise the
  test suite only covers one half of the production matrix.

### Logs

- Log decisions inline with a short why. Reviewers should not
  have to reconstruct intent from the diff alone.
- Logs that fire on every call are noise. Logs that fire on
  every error are signal. Aim the log output at the signal-to-
  noise ratio a future on-call engineer would want.
- Log lines should be parseable by both humans and machines.
  Structured logs with key=value pairs win over free-form prose
  the moment a second consumer reads them.
- Don't log secrets. The redaction layer is a safety net, not
  a primary defense.

### Dependencies

- Pin direct dependencies; let transitive dependencies float
  within their semver-compatible range. Pinning everything makes
  upgrades a chore; pinning nothing makes builds non-reproducible.
- A dependency added for one feature is a dependency the whole
  project carries. Before adding one, check whether the standard
  library or an existing transitive dependency already covers it.
- Removing an unused dependency is always cheaper than auditing
  it for the next CVE. Periodic dependency audits should ask
  "what would happen if we removed this?" not "is there a CVE?"

### Determinism and reproducibility

- Default to deterministic behavior. Random ids, timestamps, and
  non-stable iteration order are debugging hazards. Seed RNGs,
  pin clocks in tests, and sort iterators where the order is
  observable.
- Caching is a determinism feature: identical inputs should
  produce identical outputs across runs, even when the
  intermediate computation is skipped.
- A non-deterministic test failing once in a hundred runs is the
  same kind of bug as a non-deterministic production failure
  happening once a quarter. Triage both, not just the second.

### Diffs and review

- The smallest diff that solves the problem is the easiest
  diff to review. Resist the urge to bundle unrelated fixes.
- A diff with a clear narrative is faster to land than a diff
  of equal size with no story. Order commits so each one is a
  self-contained step that compiles, tests, and passes review
  on its own.
- Comments in the diff describe the change for the reviewer;
  comments in the code describe the code for the next reader.
  These are different audiences with different needs and rarely
  benefit from the same prose.

### Working with humans

- The user is the source of truth about what they want. When
  the request is ambiguous, ask. When the request is
  contradictory, surface the contradiction rather than guessing
  which side to follow.
- Acknowledge what was done, what failed, and what's next in
  short sentences. A bulleted status update is faster to read
  than a paragraph of prose.
- Never claim a task is done before it actually is. Tests
  passing is a partial signal, not a guarantee that the feature
  works end-to-end. Tests passing plus a manual exercise of the
  golden path is closer to done.
- Defer to the user's judgment about scope. A feature you find
  premature is the feature the user is paying for; build it.
- Confidence calibration matters more than confidence level.
  An expert who knows the limits of their knowledge is more
  useful than a confident generalist who doesn't.

### Migration patterns

- A long-running migration should be reversible at every step.
  A migration that requires a full rollback is a migration that
  blocks deploys. Design forward-compatible changes that allow
  the old and new shapes to coexist for at least one release.
- Database migrations on a table with live writers need extra
  care. Add columns with default values before backfilling;
  backfill in batches with progress logging; only enforce
  NOT NULL after the backfill completes.
- Renaming a public API is a multi-step process: introduce the
  new name, deprecate the old one, update internal callers,
  wait at least one release, then remove the old name. Skipping
  steps breaks downstream consumers.

### Security postures

- Treat user-provided input as hostile until proven otherwise.
  This includes file paths from the user, shell commands the
  agent constructs, and URLs the agent fetches. Validation at
  the boundary is non-negotiable.
- Never log secrets. Never embed secrets in error messages.
  Never echo secrets to the terminal. The blast radius of an
  accidentally-logged secret is hours of remediation work for
  a feature that took minutes to build.
- A path that uses `..` segments or absolute paths needs to be
  resolved through the workspace API, not concatenated. The
  workspace API is the bottleneck for path-escape attacks;
  bypassing it is the bug.
- Cryptographic operations should use library primitives, not
  hand-rolled equivalents. Even competent implementations of
  cryptographic protocols leak through side channels; library
  primitives have been audited for those.

### Data shape choices

- Choose the simplest data shape that the use case demands.
  A dict is simpler than a class; a class is simpler than a
  framework; a framework is simpler than a custom DSL. Move
  up the ladder only when the current rung is genuinely
  insufficient.
- Sum types (tagged unions) beat boolean flags when the cases
  are mutually exclusive. `status: enum` is clearer than
  `is_pending: bool, is_complete: bool, is_failed: bool`,
  which permits invalid combinations the enum forbids.
- A field that is "optional" because the caller might not have
  the data is different from a field that is "optional" because
  the schema is still evolving. The first wants `Optional[T]`;
  the second wants a schema migration.

### Documentation hygiene

- Documentation rots faster than code. A README that describes
  the architecture as it was three months ago is misinformation,
  not documentation. Either keep it current or remove it.
- Inline comments that describe what the next line does are
  noise. Inline comments that describe why the next line is
  necessary (a workaround for a specific bug, a constraint
  imposed by an external system) earn their keep.
- The first paragraph of a function's docstring should describe
  what the function does in the third person. The body of the
  docstring describes the contract — inputs, outputs, side
  effects, exceptions. Implementation notes go in inline
  comments, not the docstring.

### Build and CI considerations

- A build that succeeds locally but fails in CI is a build that
  depends on undocumented local state. Find what's different
  about CI and either replicate it locally or eliminate the
  dependency.
- A CI step that runs in twenty seconds locally but two minutes
  in CI is hiding work. Inspect what's actually happening on
  the slow runner before scaling it up.
- Caches in CI are double-edged. They speed up clean builds but
  hide test pollution. Periodically invalidate the cache to
  prove the build still works from scratch.
- Failing tests that are flaky in CI should be quarantined
  rather than retried. Retries paper over the failure mode
  without identifying it.

### API design

- A new public API should solve a specific use case for a
  specific caller. APIs without a first consumer are
  speculation; the first consumer reshapes the API in ways the
  designer didn't anticipate.
- Names in a public API outlive the team that designed them.
  Pick names that describe the contract, not the implementation.
  The implementation is the part allowed to change.
- Default parameters that change behavior in non-obvious ways
  are landmines. A default that's "safe but slow" is fine; a
  default that's "fast but only correct in some cases" is not.
- Errors are part of the API contract. Add new error cases
  carefully — every existing caller has implicitly opted into
  catching only the errors that existed at the time they wrote
  their handler.
"""


def _pad_stable_prefix_for_cache(
    *,
    stable_prefix: str,
    adapter,
    tools,
    skill_store,
    min_tokens: int = MIN_CACHEABLE_PREFIX_TOKENS,
    max_tokens: int = MAX_CACHEABLE_PREFIX_TOKENS,
) -> tuple[str, list]:
    """Ensure the stable prefix tokenizes above the cache floor.

    Anthropic silently drops cache_control markers when the cached prefix
    is below the per-model floor (haiku-4-5 ≈4000 actual tokens; docs
    say 2048 for haiku and 1024 for sonnet/opus, but the haiku effective
    floor is higher). This pads `stable_prefix` with deterministic,
    byte-stable content until the estimate clears `min_tokens` or the
    `max_tokens` upper bound is reached.

    Padding source order (per context-assembler.md §5.1):

    1. Skill bodies in name-ascending order — substantive content the
       agent might activate anyway via `skill_load`.
    2. `_OPERATING_CONTEXT_PADDING` — the static universal fallback.

    Returns the padded prefix plus the list of `Skill` objects whose
    bodies were inlined as padding. Callers use the second value to
    (a) emit `skill.loaded(load_reason="always")` events per
    context-assembler.md v3 §5.2.2 and (b) populate the activation
    registry so subsequent `skill_load` calls on those skills return a
    pointer instead of re-injecting the body.

    The returned prefix is byte-stable turn-to-turn within a session
    because: (a) padding sources are module-level constants and sorted
    frozen collections, (b) truncation uses character offsets computed
    from inputs that don't change within a session.
    """
    current = adapter.estimate_input_tokens([], tools, stable_prefix)
    inlined_skills: list = []
    if current >= min_tokens:
        return stable_prefix, inlined_skills

    headroom_tokens = max_tokens - current
    headroom_chars = max(0, headroom_tokens * 4)
    if headroom_chars <= 0:
        return stable_prefix, inlined_skills

    segments: list[str] = []
    used_chars = 0

    if skill_store is not None and len(skill_store) > 0:
        for skill in sorted(skill_store.list_skills(), key=lambda s: s.name):
            remaining = headroom_chars - used_chars
            if remaining < 200:
                break
            body = skill.body.strip()
            segment = f"### Skill: {skill.name}\n\n{body}\n"
            if len(segment) > remaining:
                segment = segment[:remaining]
            segments.append(segment)
            used_chars += len(segment)
            inlined_skills.append(skill)

    remaining = headroom_chars - used_chars
    if remaining >= 200:
        ops = _OPERATING_CONTEXT_PADDING
        if len(ops) > remaining:
            ops = ops[:remaining]
        segments.append(ops)
        used_chars += len(ops)

    if not segments:
        return stable_prefix, inlined_skills
    return (
        stable_prefix.rstrip() + "\n\n" + "".join(segments).rstrip() + "\n",
        inlined_skills,
    )


class UnknownAliasError(ValueError):
    """The user typed `@<alias>` but the alias isn't registered."""

    def __init__(self, alias: str) -> None:
        super().__init__(f"unknown model alias: {alias!r}")
        self.alias = alias


class OverrideError(ValueError):
    """Malformed per-message override (routing-engine.md §9.2).

    Raised when `@<alias>` opens the message but the trailing-whitespace +
    body requirement isn't met — i.e. the message is just `@haiku` with
    nothing after it. The turn does not start.
    """

    def __init__(self, alias: str) -> None:
        super().__init__(f"override @{alias} requires a message body after the alias (spec §9.2)")
        self.alias = alias


class AmbiguousModelError(ValueError):
    """The user's model input matches multiple registered canonical ids.

    Raised by `/model` resolution when a suffix match returns 2+ candidates.
    Carries the candidate list so the CLI/TUI can prompt for clarification.
    """

    def __init__(self, input: str, candidates: list[str]) -> None:
        super().__init__(
            f"ambiguous model {input!r}; matches {len(candidates)} ids: {', '.join(candidates)}"
        )
        self.input = input
        self.candidates = list(candidates)


@dataclass(frozen=True)
class TurnResult:
    turn_id: str
    chosen_model: str
    stop_reason: StopReason
    assistant_text: str
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    llm_call_count: int
    tool_call_count: int
    wall_time_seconds: float


class UserExplicitModelRejectedError(Exception):
    """Raised when the user's explicit model choice — a per-message
    ``@model`` override or the session sticky model set via ``/model`` —
    fails routing capability validation.

    Without this check, routing silently falls through to the next chain
    slot (typically the global default), so the user is billed for a model
    they didn't pick. The clear UX is to refuse the turn and tell the user
    why so they can switch model, clear the sticky, or change the turn so
    the missing capability isn't needed.

    The ``route.decided`` event still records the rejection in the trace.
    """

    def __init__(
        self,
        *,
        source: str,
        model: str,
        validation_failure: str,
        would_fall_back_to: str | None,
    ) -> None:
        self.source = source
        self.model = model
        self.validation_failure = validation_failure
        self.would_fall_back_to = would_fall_back_to
        fallback_phrase = (
            f" (would have fallen back to {would_fall_back_to})" if would_fall_back_to else ""
        )
        super().__init__(
            f"{source} {model} can't handle this turn: {validation_failure}"
            f"{fallback_phrase}. "
            f"Pick a different model, clear the sticky with `/model -`, or "
            f"adjust the turn (e.g. drop tools/images)."
        )


# Callback signature for live streaming events. May be sync or async.
StreamHandler = Callable[[StreamingEvent], Awaitable[None] | None]


# Tool names forbidden inside worker sessions (delegation.md §5.6). Workers
# never see these in their effective tool list, regardless of what the
# planner asked for via `allowed_tools`.
_WORKER_FORBIDDEN_TOOLS: frozenset[str] = frozenset(
    {"delegate", "memory_add", "memory_replace", "memory_consolidate"}
)


def _estimate_task_tokens(request: DelegateRequest) -> int:
    """Heuristic token count for the worker's synthetic user message.

    Matches the `chars/4` heuristic used elsewhere in the manager. Includes
    the task brief and the explicit context references so the trace event
    reflects the real input size the worker faces.
    """
    chars = len(request.task)
    for ref in request.context.include:
        chars += len(ref) + 2  # newline separators
    return max(1, chars // 4)


def _worker_system_prompt(*, base: str, task: str, context) -> str:
    """Compose the worker's stable system prompt.

    Per delegation.md §5.7, the prompt is the base persona plus a
    worker-specific instruction block. We deliberately keep this byte-stable
    per worker session so the provider's cache_control marker fires across
    the worker's tool cycles. The task brief is NOT inlined here — it's the
    synthetic user message of the worker's first turn (§5.7 step 3-4).
    """
    extras = ""
    if context.mode == "explicit" and context.include:
        joined = "\n".join(f"- {ref}" for ref in context.include)
        extras = "\n\nThe planner provided the following context references:\n" + joined
    return (
        base.rstrip()
        + "\n\n## Worker mode\n"
        + "You are a focused worker LLM spawned by a planner. Be terse, "
        + "complete the task, and return — do not ask clarifying questions. "
        + "If the task can't be completed with the information given, return "
        + "a short statement of what's missing rather than guessing."
        + extras
    )


def _stop_reason_to_failure_mode(stop_reason: StopReason):
    """Map an unusable adapter stop reason to a `DelegateFailureMode`."""
    if stop_reason == StopReason.MAX_TOKENS:
        return "max_tokens_exceeded"
    if stop_reason == StopReason.CANCELLED:
        return "cancelled_by_user"
    return "worker_error"


class SessionManager:
    """Coordinates routing, adapter calls, and tool dispatch for a session."""

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        routing: RoutingEngine,
        dispatcher: ToolDispatcher,
        bus: EventBus,
        store: SessionStore,
        pricing: PriceTable,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        global_default_model: str = "anthropic:claude-sonnet-4-6",
        workspace_default_model: str | None = None,
        max_output_tokens: int = 4096,
        memory_factory: Callable[[str], MemoryStore] | None = None,
        skill_store_factory: Callable[[str], Any] | None = None,
        fingerprint_inputs_hook: Callable[[str, TurnContext], Awaitable[None] | None] | None = None,
    ) -> None:
        self._registry = registry
        self._routing = routing
        self._dispatcher = dispatcher
        self._bus = bus
        self._store = store
        self._pricing = pricing
        self._system_prompt = system_prompt
        self._global_default_model = global_default_model
        self._workspace_default_model = workspace_default_model
        self._max_output_tokens = max_output_tokens
        # Per-session bidirectional tool id maps (canonical-format §6.2).
        self._tool_id_maps: dict[str, ToolIdMap] = {}
        # Per-session memory stores (Phase 2 bounded MEMORY.md / USER.md).
        # None means the session has no memory; the memory tools will refuse.
        self._memory_factory = memory_factory
        self._memory_stores: dict[str, MemoryStore | None] = {}
        # Per-session skill stores. None means the session has no skills;
        # skill tools refuse to run.
        self._skill_store_factory = skill_store_factory
        self._skill_stores: dict[str, Any] = {}
        # Per-session skill activation registry (context-assembler.md v3
        # §5.2). Tracks pre-activated + explicitly-activated skills and
        # enforces the §5.2.4 budget caps on `skill_load`.
        self._skill_activations: dict[str, SkillActivationRegistry] = {}
        # Cached stable system prompt (base persona + discovery index +
        # v2 §5.1 padding) per session. Pre-computed at session init so
        # the turn loop never re-runs padding (which would risk
        # byte-drift if model selection varies across turns).
        self._stable_prompt_cache: dict[str, str] = {}
        # /share state — captures the most recent slash-command output per
        # session so the next user message can include it. See `/share` in
        # the CLI/TUI. One-shot: cleared on consumption.
        self._slash_buffers: dict[str, str] = {}
        self._share_pending: set[str] = set()
        self._fingerprint_inputs_hook = fingerprint_inputs_hook
        # Per-worker-session resolved tier model, populated by spawn_worker
        # before submit_turn so the engine's slot 5 reads it from TurnContext
        # (delegation.md §7). Cleared after the worker's session ends.
        self._worker_tier_models: dict[str, str] = {}

    # ---- Session lifecycle --------------------------------------------

    def create_session(self, *, workspace_path: str, active_model: str | None = None) -> Session:
        # Resolve aliases to canonical ids for consistency with set_active_model.
        resolved: str | None = None
        if active_model is not None:
            resolved = self._registry.resolve_alias(active_model) or active_model
            if not self._registry.is_configured(resolved):
                raise UnknownAliasError(active_model)
        session = self._store.create_session(workspace_path=workspace_path, active_model=resolved)
        self._tool_id_maps[session.id] = ToolIdMap()
        if self._memory_factory is not None:
            self._memory_stores[session.id] = self._memory_factory(workspace_path)
        else:
            self._memory_stores[session.id] = None
        if self._skill_store_factory is not None:
            self._skill_stores[session.id] = self._skill_store_factory(workspace_path)
        else:
            self._skill_stores[session.id] = None
        # context-assembler.md v3 §5.2: pre-compute the stable system
        # prompt + run pre-activation. This happens AFTER the session is
        # created (so the FK on the emitted events is valid) and BEFORE
        # any `turn.started` (so the events stand outside any turn).
        self._initialize_skill_activations(session)
        return session

    def _initialize_skill_activations(self, session: Session) -> None:
        """Compute the stable system prompt once per session and emit
        pre-activation events for any skill bodies inlined as padding.

        Per context-assembler.md v3 §5.2.2:
          - Compute the stable prefix (base + index + v2 §5.1 padding).
          - For each inlined skill body, record the skill in the
            activation registry and emit `skill.loaded` with
            `load_reason="always"` and `triggered_by_tool_use_id=None`.
          - Annotate the rendered discovery-index line with `[preloaded]`
            so the agent knows the body is already in the prompt.
        """
        registry = SkillActivationRegistry()
        self._skill_activations[session.id] = registry
        skill_store = self._skill_stores.get(session.id)
        # Pick a "seed" adapter for the token estimate. The padding
        # logic is adapter-independent in practice (all three adapters
        # use the same `~chars/4` heuristic per
        # provider-adapter-contract §3.1) so any registered adapter
        # works. If no adapter is registered, fall back to a heuristic
        # so the session still gets a stable prefix.
        seed_model = (
            session.active_model
            or (
                self._registry.resolve_alias(self._workspace_default_model)
                if self._workspace_default_model
                else None
            )
            or self._registry.resolve_alias(self._global_default_model)
            or self._global_default_model
        )
        seed_adapter = (
            self._registry.adapter_for(seed_model)
            if seed_model in self._registry
            else _HeuristicAdapter()
        )
        tool_definitions = self._effective_tool_definitions(session)
        prefix, inlined = _assemble_stable_system_prompt(
            base_prompt=self._system_prompt,
            skill_store=skill_store,
            adapter=seed_adapter,
            tools=tool_definitions,
        )
        self._stable_prompt_cache[session.id] = prefix
        for skill in inlined:
            registry.mark_preloaded(skill.name)
            self._bus.emit(
                make_event(
                    type="skill.loaded",
                    session_id=session.id,
                    turn_id=None,
                    actor=Actor.SYSTEM,
                    payload=SkillLoaded(
                        skill_id=skill.name,
                        skill_version=skill.version,
                        load_reason="always",
                        load_size_tokens=skill.estimated_body_tokens,
                        source=skill.source,
                        triggered_by_tool_use_id=None,
                    ),
                    timestamp=_now(),
                )
            )

    def get_session(self, session_id: str) -> Session:
        """Return the current Session record from the store.

        Always re-reads from `SessionStore` — never returns a cached copy.
        Callers that hold a long-lived Session reference (REPL, TUI) should
        call this after any mutation (`set_active_model`, post-turn updates)
        to avoid showing stale fields like `active_model` or `cost_so_far_usd`.
        """
        return self._store.get_session(session_id)

    def memory_for(self, session_id: str) -> MemoryStore | None:
        """Return the per-session memory store, if memory is configured."""
        return self._memory_stores.get(session_id)

    @property
    def pricing_version(self) -> str:
        return self._pricing.version

    def routing_policy_version(self) -> str | None:
        """Return the loaded routing policy's opaque version, or None.

        Surfaces via `GET /sessions/{id}.routing_policy_version` so clients
        can label which rules are active and notice when the file changes.
        Returns `None` for workspaces (deployments, really — v1 has one
        policy per process) without a loaded `~/.metis/routing.yaml`.
        """
        return self._routing.policy.version

    def skills_for(self, session_id: str) -> Any:
        """Return the per-session skill store, if skills are configured."""
        return self._skill_stores.get(session_id)

    def skill_activations_for(self, session_id: str) -> SkillActivationRegistry | None:
        """Return the per-session activation registry (context-assembler.md
        v3 §5.2). Useful for tests + introspection; the production hot
        path uses the registry through `ToolContext.skill_activations`."""
        return self._skill_activations.get(session_id)

    def stable_system_prompt_for(self, session_id: str) -> str:
        """Return the cached stable system prompt for this session.

        The prompt is composed once at `create_session` time (base
        persona + discovery index + v2 §5.1 padding) and reused on every
        LLM call so the provider's cache_control marker fires. Visible
        for tests; the turn loop reads `self._stable_prompt_cache`
        directly to avoid the dict lookup hop.
        """
        return self._stable_prompt_cache.get(session_id, self._system_prompt)

    # ---- /share bridge ------------------------------------------------

    def buffer_slash_output(self, session_id: str, text: str) -> None:
        """Capture the rendered output of a slash command so the user can
        later run `/share` to inject it into the next turn's context.

        Buffers per-session; each call overwrites any prior buffer. The
        agent doesn't see the buffer until the user explicitly opts in via
        `/share` — slash commands are otherwise local to the client.
        """
        if text:
            self._slash_buffers[session_id] = text

    def mark_share_pending(self, session_id: str) -> str | None:
        """Flag the buffered slash output for inclusion in the next turn.

        Returns the buffered text (for the caller to render a preview /
        confirmation), or None if nothing has been buffered yet.
        """
        text = self._slash_buffers.get(session_id)
        if text is None:
            return None
        self._share_pending.add(session_id)
        return text

    def consume_pending_share(self, session_id: str) -> str | None:
        """Return the buffered slash output if `/share` was pending, then
        clear the flag. Internal: called by `submit_turn` at turn start.

        Does NOT clear the buffer itself — subsequent slash commands
        overwrite it normally. Only the one-shot pending flag clears.
        """
        if session_id not in self._share_pending:
            return None
        self._share_pending.discard(session_id)
        return self._slash_buffers.get(session_id)

    def set_active_model(self, session_id: str, model: str | None) -> str | None:
        """Apply a /model command. `None` clears the Active model.

        Returns the resolved canonical id (or None when cleared) so callers
        can display the resolution result without re-reading their stale
        local Session reference.

        Resolution policy for non-None inputs:

        1. Exact alias / canonical-id match → use it.
        2. Boundary-respecting suffix match (`ModelRegistry.find_by_suffix`):
           - Exactly one match: auto-resolve. Common case for users typing
             `openai/gpt-oss-20b` instead of `openrouter:openai/gpt-oss-20b`.
           - Two or more matches: raise `AmbiguousModelError` carrying the
             candidate list so the caller can prompt for clarification.
        3. No match anywhere: raise `UnknownAliasError`.
        """
        session = self._store.get_session(session_id)
        if model is not None:
            resolved = self._resolve_model_input(model)
            session.active_model = resolved
        else:
            session.active_model = None
        self._store.update_session(session)
        return session.active_model

    def _resolve_model_input(self, input: str) -> str:
        """Lookup policy for `/model <input>`. See `set_active_model` docs."""
        direct = self._registry.resolve_alias(input)
        if direct is not None:
            return direct
        candidates = self._registry.find_by_suffix(input)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise AmbiguousModelError(input, candidates)
        raise UnknownAliasError(input)

    # ---- Tool visibility (delegation.md §5.6) -------------------------

    def _effective_tool_definitions(self, session: Session):
        """Return tool definitions visible to this session.

        Delegation isolation rules (delegation.md §5.6):
        - Worker sessions never see `delegate` or memory tools.
        - Top-level sessions whose active model has `can_delegate=False`
          don't see `delegate` (Phase-4 default in the spec; honest to the
          planner about what's actually available).

        Other surfaces (HTTP, gateway) keep the dispatcher-wide view via
        `dispatcher.get_definitions_for_session`.
        """
        raw = self._dispatcher.get_definitions_for_session(session)
        if session.is_worker:
            return [d for d in raw if d.name not in _WORKER_FORBIDDEN_TOOLS]
        if session.active_model and not self._registry.can_delegate(session.active_model):
            return [d for d in raw if d.name != "delegate"]
        if session.active_model is None:
            # No sticky model: the active model is resolved per-turn. Default
            # to hiding `delegate` so unconfigured top-level sessions don't
            # surface a tool that may not be usable.
            return [d for d in raw if d.name != "delegate"]
        return raw

    # ---- Worker spawn (delegation.md §6.1) ----------------------------

    async def spawn_worker(self, request: DelegateRequest) -> DelegateOutcome:
        """Resolve the tier, spawn a worker session, run it to completion.

        Synchronous-blocking per v1 MVP (delegation.md §2.2.2). Concurrency,
        cancellation cascade, and worker streaming are deferred.

        Emits `delegate.started` once the worker session exists (so the
        worker_session_id can be attached). Failure to resolve a model for
        the tier short-circuits with `no_model_available_for_tier` and emits
        nothing here — the `delegate()` tool body emits `delegate.failed`.
        """
        resolved_model = self._registry.model_for_tier(request.tier)
        empty_usage = DelegateUsageSummary(
            model=resolved_model or "",
            turn_count=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            wall_time_seconds=0.0,
            tool_call_count=0,
        )
        if resolved_model is None:
            return DelegateOutcome(
                worker_session_id="",
                success=False,
                output="",
                error=f"no model registered for tier {request.tier!r}",
                failure_mode="no_model_available_for_tier",
                usage_summary=empty_usage,
                allowed_tool_count=0,
                task_size_tokens=_estimate_task_tokens(request),
            )

        parent_session = self._store.get_session(request.parent_session_id)
        # delegation.md §5.2: workers don't inherit a sticky model. The
        # routing chain enters slot 5 fresh with the resolved tier model as
        # the candidate; slots 1-3 typically return `not_applicable`.
        worker_session = self._store.create_session(
            workspace_path=parent_session.workspace_path,
            active_model=None,
            parent_session_id=request.parent_session_id,
            parent_tool_use_id=request.parent_tool_use_id,
            is_worker=True,
        )
        self._tool_id_maps[worker_session.id] = ToolIdMap()
        self._memory_stores[worker_session.id] = None
        self._skill_stores[worker_session.id] = self._skill_stores.get(request.parent_session_id)
        self._skill_activations[worker_session.id] = SkillActivationRegistry()
        self._stable_prompt_cache[worker_session.id] = _worker_system_prompt(
            base=self._system_prompt,
            task=request.task,
            context=request.context,
        )
        self._worker_tier_models[worker_session.id] = resolved_model

        allowed_count = len(request.allowed_tools) if request.allowed_tools is not None else 0
        requested_tools = set(request.allowed_tools or ())
        dropped = tuple(sorted(_WORKER_FORBIDDEN_TOOLS & requested_tools))

        task_size_tokens = _estimate_task_tokens(request)
        self._bus.emit(
            make_event(
                type="delegate.started",
                session_id=request.parent_session_id,
                turn_id=None,
                actor=Actor.SYSTEM,
                payload=DelegateStarted(
                    tool_use_id=request.parent_tool_use_id,
                    worker_session_id=worker_session.id,
                    tier=request.tier,
                    resolved_model=resolved_model,
                    context_mode=request.context.mode,
                    context_reference_count=len(request.context.include),
                    task_size_tokens=task_size_tokens,
                    allowed_tool_count=allowed_count,
                    dropped_tools=list(dropped),
                ),
                timestamp=_now(),
            )
        )

        try:
            result = await self.submit_turn(worker_session.id, request.task)
        except RoutingError as exc:
            self._cleanup_worker(worker_session.id)
            return DelegateOutcome(
                worker_session_id=worker_session.id,
                success=False,
                output="",
                error=f"worker routing failed: {exc}",
                failure_mode="worker_error",
                usage_summary=empty_usage,
                dropped_tools=dropped,
                allowed_tool_count=allowed_count,
                task_size_tokens=task_size_tokens,
            )
        except Exception as exc:
            self._cleanup_worker(worker_session.id)
            return DelegateOutcome(
                worker_session_id=worker_session.id,
                success=False,
                output="",
                error=f"worker_error: {exc}",
                failure_mode="worker_error",
                usage_summary=empty_usage,
                dropped_tools=dropped,
                allowed_tool_count=allowed_count,
                task_size_tokens=task_size_tokens,
            )

        usage = DelegateUsageSummary(
            model=result.chosen_model,
            turn_count=1,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            wall_time_seconds=result.wall_time_seconds,
            tool_call_count=result.tool_call_count,
        )
        success = result.stop_reason == StopReason.END_TURN
        failure_mode = None if success else _stop_reason_to_failure_mode(result.stop_reason)
        if result.stop_reason == StopReason.MAX_TOKENS:
            error = "max_tokens_exceeded"
        elif not success:
            error = f"worker stopped with {result.stop_reason.value}"
        else:
            error = None

        self._cleanup_worker(worker_session.id)

        return DelegateOutcome(
            worker_session_id=worker_session.id,
            success=success,
            output=result.assistant_text,
            error=error,
            failure_mode=failure_mode,
            usage_summary=usage,
            dropped_tools=dropped,
            allowed_tool_count=allowed_count,
            task_size_tokens=task_size_tokens,
        )

    def _cleanup_worker(self, worker_session_id: str) -> None:
        """Drop per-worker book-keeping that doesn't outlive the spawn call."""
        self._worker_tier_models.pop(worker_session_id, None)
        self._tool_id_maps.pop(worker_session_id, None)
        self._memory_stores.pop(worker_session_id, None)
        self._skill_stores.pop(worker_session_id, None)
        self._skill_activations.pop(worker_session_id, None)
        self._stable_prompt_cache.pop(worker_session_id, None)

    # ---- Turn loop ----------------------------------------------------

    async def submit_turn(
        self,
        session_id: str,
        user_text: str,
        *,
        on_streaming_event: StreamHandler | None = None,
        temperature: float | None = None,
        workload_id: str | None = None,
    ) -> TurnResult:
        session = self._store.get_session(session_id)
        turn_id = str(ULID())
        loop_start = asyncio.get_event_loop().time()

        # 1. Parse per-message override.
        override = parse_per_message_override(user_text, self._registry)
        if override.body_missing:
            raise OverrideError(override.raw_alias or "")
        if override.is_unknown_alias:
            raise UnknownAliasError(override.raw_alias or "")

        # 2. If `/share` was pending, prepend the buffered slash-command
        #    output to the user message. One-shot: the flag clears here.
        #    The composed text is what gets persisted as the user Message
        #    so the agent's behavior is reconstructible from history.
        message_text = override.cleaned_text
        shared = self.consume_pending_share(session_id)
        if shared:
            message_text = _compose_message_with_shared(shared, message_text)

        # 3. Add user message to the session.
        user_message = Message(
            id=new_message_id(),
            session_id=session_id,
            role=Role.USER,
            content=[TextBlock(text=message_text)],
            created_at=_now(),
        )
        self._store.add_message(session_id, user_message)

        # 3. Emit turn.started.
        history = self._store.get_messages(session_id)
        tool_definitions = self._effective_tool_definitions(session)
        ctx = self._build_turn_context(
            session_id=session_id,
            turn_id=turn_id,
            history=history,
            tool_definitions=tool_definitions,
            session=session,
            override=override,
            workload_id=workload_id,
        )
        if self._fingerprint_inputs_hook is not None:
            try:
                hook_result = self._fingerprint_inputs_hook(turn_id, ctx)
                if inspect.isawaitable(hook_result):
                    # v2 hooks await embedding compute here (pattern-store.md
                    # §16; benchmarks/RESULTS.md §A3-rev4 Q1). Awaiting BEFORE
                    # turn.started fires keeps the eval cascade race surface
                    # closed: no `turn.completed` is in flight yet, so an
                    # `eval.completed` cannot race ahead of `_turn_outcomes`
                    # being set by the pattern subscriber.
                    await hook_result
            except Exception:
                logger.exception("fingerprint_inputs_hook failed for turn %s", turn_id)
        turn_started_event = self._emit_turn_started(
            session_id=session_id,
            turn_id=turn_id,
            user_message=user_message,
            history=history,
            ctx=ctx,
        )

        # 4. Route. Hard-failure here propagates without any LLM/tool events.
        try:
            decision = self._routing.decide(ctx)
        except RoutingError:
            self._emit_turn_cancelled(
                session_id,
                turn_id,
                reason="timeout",
                partial_llm_calls=0,
                partial_tool_calls=0,
            )
            raise

        # Refuse to silently fall through when the user picked a model
        # explicitly (@override or sticky) and it failed validation. The
        # `route.decided` event already carries the rejection in the trace;
        # we just don't proceed to call a different model behind their back.
        explicit_rejection = _user_explicit_rejection(decision)
        if explicit_rejection is not None:
            raise explicit_rejection

        chosen_model = decision.chosen_model
        provider = self._registry.provider_of(chosen_model)
        adapter = self._registry.adapter_for(chosen_model)

        # 5. Tool-cycle loop with the turn-locked model.
        llm_calls = 0
        tool_calls = 0
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = Decimal("0")
        final_stop_reason = StopReason.END_TURN
        last_assistant_text = ""
        parent_event_id = turn_started_event

        memory = self._memory_stores.get(session_id)
        skill_store = self._skill_stores.get(session_id)
        skill_activations = self._skill_activations.get(session_id)

        try:
            while True:
                history = self._store.get_messages(session_id)
                # Split the system prompt into the two segments the cache
                # breakpoint sits between (see context-assembler.md §2-§5):
                #   stable: base persona + skill discovery index + v2 §5.1
                #           padding (precomputed at session init, cached
                #           byte-stable across turns)
                #   volatile: USER.md + MEMORY.md (mutates per turn)
                # The stable prefix is the same bytes turn-to-turn so the
                # provider's cache_control marker actually fires — see
                # context-assembler.md §5.1 for the floor / padding rule.
                stable_system_prompt = self._stable_prompt_cache.get(
                    session_id, self._system_prompt
                )
                turn_memory = self._memory_stores.get(session_id)
                volatile_system_prompt = (
                    _assemble_volatile_memory(turn_memory) if turn_memory is not None else None
                )
                # Estimate uses the combined text for budget purposes; the
                # adapter only sees the two segments separately.
                combined_for_estimate = (
                    f"{stable_system_prompt}\n\n{volatile_system_prompt}"
                    if volatile_system_prompt
                    else stable_system_prompt
                )
                request = CanonicalRequest(
                    request_id=new_message_id(),
                    messages=history,
                    tools=tool_definitions,
                    system_prompt=stable_system_prompt,
                    model=chosen_model,
                    max_output_tokens=self._max_output_tokens,
                    temperature=temperature,
                    tool_id_map=self._tool_id_maps.get(session_id),
                    system_prompt_volatile=volatile_system_prompt,
                    workspace_path=session.workspace_path,
                )
                est_tokens = adapter.estimate_input_tokens(
                    history, tool_definitions, combined_for_estimate
                )

                llm_started_event = self._emit_llm_call_started(
                    session_id=session_id,
                    turn_id=turn_id,
                    model=chosen_model,
                    provider=provider,
                    request_id=request.request_id,
                    estimated_tokens=est_tokens,
                    parent_event_id=parent_event_id,
                    is_worker=session.is_worker,
                    parent_session_id=session.parent_session_id,
                )
                try:
                    final = await _consume_stream(adapter.stream(request), on_streaming_event)
                except AdapterError as exc:
                    self._routing.availability.mark_failure(provider, chosen_model, exc.error_class)
                    self._emit_llm_call_failed(
                        session_id=session_id,
                        turn_id=turn_id,
                        model=chosen_model,
                        provider=provider,
                        exc=exc,
                        parent_event_id=llm_started_event,
                    )
                    if isinstance(exc, CancelledError):
                        self._emit_turn_cancelled(
                            session_id,
                            turn_id,
                            reason="user_cancel",
                            partial_llm_calls=llm_calls,
                            partial_tool_calls=tool_calls,
                        )
                    raise
                else:
                    self._routing.availability.mark_success(provider, chosen_model)

                llm_calls += 1
                total_input_tokens += final.usage.input_tokens
                total_output_tokens += final.usage.output_tokens
                cost = self._pricing.compute_cost(chosen_model, final.usage)
                total_cost += cost

                # Build the assistant message with full metadata.
                assistant_message = Message(
                    id=new_message_id(),
                    session_id=session_id,
                    role=Role.ASSISTANT,
                    content=final.final_content,
                    created_at=_now(),
                    metadata=MessageMetadata(
                        model=chosen_model,
                        provider=provider,
                        routing=RoutingDecisionRecord(
                            mode=_mode_for_chain_index(decision.winner_index),
                            chosen_model=chosen_model,
                            reason=decision.chain[decision.winner_index].reason,
                            rule_name=decision.chain[decision.winner_index].rule_name,
                        ),
                        usage=Usage(
                            input_tokens=final.usage.input_tokens,
                            output_tokens=final.usage.output_tokens,
                            cached_input_tokens=final.usage.cached_input_tokens,
                            cache_creation_input_tokens=(final.usage.cache_creation_input_tokens),
                            cost_usd=cost,
                            pricing_version=self._pricing.version,
                            latency_ms=final.latency_ms,
                        ),
                        status=MessageStatus.COMPLETE,
                    ),
                )
                self._store.add_message(session_id, assistant_message)
                last_assistant_text = _assistant_text(final.final_content) or last_assistant_text

                self._emit_llm_call_completed(
                    session_id=session_id,
                    turn_id=turn_id,
                    model=chosen_model,
                    provider=provider,
                    usage=final.usage,
                    cost=cost,
                    latency_ms=final.latency_ms,
                    stop_reason=final.stop_reason,
                    response_content=final.final_content,
                    parent_event_id=llm_started_event,
                    parent_session_id=session.parent_session_id,
                )

                # 6. Decide whether to dispatch tools and continue, or stop.
                if final.stop_reason != StopReason.TOOL_USE:
                    final_stop_reason = final.stop_reason
                    break

                # Parallel-dispatch all tool_use blocks; collect results.
                tool_uses = [b for b in final.final_content if isinstance(b, ToolUseBlock)]
                if not tool_uses:
                    final_stop_reason = StopReason.END_TURN
                    break

                # Snapshot memory hashes before tool dispatch so we can
                # detect mutations performed by memory tools and emit
                # memory.updated events.
                memory_before = _memory_hashes(memory)

                results = await asyncio.gather(
                    *[
                        self._dispatcher.dispatch(
                            tu,
                            session_id=session_id,
                            turn_id=turn_id,
                            workspace_path=session.workspace_path,
                            parent_event_id=llm_started_event,
                            memory=memory,
                            skills=skill_store,
                            skill_activations=skill_activations,
                            worker_spawner=self,
                            is_worker=session.is_worker,
                        )
                        for tu in tool_uses
                    ]
                )
                tool_calls += len(results)

                # Emit memory.updated / memory.eviction events for any file
                # that changed during this batch of tool calls.
                self._emit_memory_events_for_changes(
                    memory_before=memory_before,
                    memory=memory,
                    tool_uses=tool_uses,
                    session_id=session_id,
                    turn_id=turn_id,
                    parent_event_id=llm_started_event,
                )

                # Each tool_result becomes its own TOOL message; the adapter
                # merges consecutive TOOL messages into one wire user message.
                for result in results:
                    tool_msg = Message(
                        id=new_message_id(),
                        session_id=session_id,
                        role=Role.TOOL,
                        content=[result],
                        created_at=_now(),
                        metadata=MessageMetadata(parent_tool_use_id=result.tool_use_id),
                    )
                    self._store.add_message(session_id, tool_msg)
                parent_event_id = llm_started_event
        finally:
            wall_time = asyncio.get_event_loop().time() - loop_start

        # 7. Update session cost/turn counters.
        session.cost_so_far_usd += float(total_cost)
        session.turn_count += 1
        self._store.update_session(session)

        # 8. Emit turn.completed.
        # First text block of the persisted user message — what the LLM
        # judge needs to read intent (evaluator.md §5.1).
        user_prompt_text = next(
            (b.text for b in user_message.content if isinstance(b, TextBlock)),
            None,
        )
        self._emit_turn_completed(
            session_id=session_id,
            turn_id=turn_id,
            stop_reason=final_stop_reason,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost=total_cost,
            wall_time=wall_time,
            parent_event_id=turn_started_event,
            final_response_text=last_assistant_text,
            user_prompt_text=user_prompt_text,
            parent_session_id=session.parent_session_id,
        )

        return TurnResult(
            turn_id=turn_id,
            chosen_model=chosen_model,
            stop_reason=final_stop_reason,
            assistant_text=last_assistant_text,
            cost_usd=total_cost,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            llm_call_count=llm_calls,
            tool_call_count=tool_calls,
            wall_time_seconds=wall_time,
        )

    # ---- Helpers ------------------------------------------------------

    def _build_turn_context(
        self,
        *,
        session_id: str,
        turn_id: str,
        history: list[Message],
        tool_definitions,
        session: Session,
        override: OverrideParseResult,
        workload_id: str | None = None,
    ) -> TurnContext:
        has_images = any(isinstance(b, ImageBlock) for m in history for b in m.content)
        # Resolve a default model id by alias if the configured default is one.
        workspace_default = (
            self._registry.resolve_alias(self._workspace_default_model)
            if self._workspace_default_model
            else None
        ) or self._workspace_default_model
        global_default = (
            self._registry.resolve_alias(self._global_default_model) or self._global_default_model
        )
        # estimate_input_tokens is provider-specific; we use the configured
        # default's adapter as a pre-routing estimate. Routing only uses
        # this to gate `exceeds_context_window` so it's an upper bound, not
        # a final number.
        seed_model = session.active_model or workspace_default or global_default
        seed_adapter = (
            self._registry.adapter_for(seed_model) if seed_model in self._registry else None
        )
        estimated_tokens = (
            seed_adapter.estimate_input_tokens(history, tool_definitions, self._system_prompt)
            if seed_adapter
            else _heuristic_token_estimate(history, self._system_prompt)
        )
        has_tool_calls_in_history = any(
            isinstance(b, ToolUseBlock) for m in history for b in m.content
        )
        user_message_text = ""
        # The new USER message is the last USER message in the (already-stored)
        # history. Pull the first text block's content out for predicate eval.
        for msg in reversed(history):
            if msg.role == Role.USER:
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        user_message_text = block.text
                        break
                break
        return TurnContext(
            session_id=session_id,
            turn_id=turn_id,
            estimated_input_tokens=estimated_tokens,
            has_images=has_images,
            has_tool_definitions=bool(tool_definitions),
            has_system_prompt=bool(self._system_prompt),
            has_tool_calls_in_history=has_tool_calls_in_history,
            per_message_override=override.resolved_model,
            session_active_model=session.active_model,
            workspace_default_model=workspace_default,
            global_default_model=global_default,
            user_message_text=user_message_text,
            workspace_path=session.workspace_path,
            workload_id=workload_id,
            worker_tier_model=self._worker_tier_models.get(session_id),
        )

    # ---- Event emitters -----------------------------------------------

    def _emit_turn_started(
        self,
        *,
        session_id: str,
        turn_id: str,
        user_message: Message,
        history: list[Message],
        ctx: TurnContext,
    ) -> str:
        user_text = ""
        for block in user_message.content:
            if isinstance(block, TextBlock):
                user_text = block.text
                break
        payload = TurnStarted(
            user_message_hash=hashlib.sha256(user_text.encode()).hexdigest(),
            estimated_input_tokens=ctx.estimated_input_tokens,
            has_images=ctx.has_images,
            has_tool_calls_in_history=any(
                any(isinstance(b, ToolUseBlock) for b in m.content) for m in history
            ),
        )
        event = make_event(
            type="turn.started",
            session_id=session_id,
            turn_id=turn_id,
            actor=Actor.USER,
            payload=payload,
            timestamp=_now(),
        )
        self._bus.emit(event)
        return event.id

    def _emit_llm_call_started(
        self,
        *,
        session_id: str,
        turn_id: str,
        model: str,
        provider: str,
        request_id: str,
        estimated_tokens: int,
        parent_event_id: str | None,
        is_worker: bool = False,
        parent_session_id: str | None = None,
    ) -> str:
        event = make_event(
            type="llm.call_started",
            session_id=session_id,
            turn_id=turn_id,
            actor=Actor.WORKER if is_worker else Actor.AGENT,
            payload=LLMCallStarted(
                model=model,
                provider=provider,
                estimated_input_tokens=estimated_tokens,
                request_id=request_id,
                is_worker=is_worker,
                parent_session_id=parent_session_id,
            ),
            timestamp=_now(),
            parent_event_id=parent_event_id,
        )
        self._bus.emit(event)
        return event.id

    def _emit_llm_call_completed(
        self,
        *,
        session_id: str,
        turn_id: str,
        model: str,
        provider: str,
        usage,
        cost: Decimal,
        latency_ms: int,
        stop_reason: StopReason,
        response_content: list[ContentBlock],
        parent_event_id: str | None,
        parent_session_id: str | None = None,
    ) -> None:
        produced_tool_calls = sum(1 for b in response_content if isinstance(b, ToolUseBlock))
        from metis_core.canonical.content import ThinkingBlock

        produced_thinking = sum(1 for b in response_content if isinstance(b, ThinkingBlock))
        self._bus.emit(
            make_event(
                type="llm.call_completed",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.WORKER if parent_session_id else Actor.AGENT,
                payload=LLMCallCompleted(
                    model=model,
                    provider=provider,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    cache_creation_input_tokens=usage.cache_creation_input_tokens,
                    cost_usd=float(cost),
                    pricing_version=self._pricing.version,
                    latency_ms=latency_ms,
                    stop_reason=stop_reason.value,  # type: ignore[arg-type]
                    produced_tool_calls=produced_tool_calls,
                    produced_thinking_blocks=produced_thinking,
                    parent_session_id=parent_session_id,
                ),
                timestamp=_now(),
                parent_event_id=parent_event_id,
            )
        )

    def _emit_llm_call_failed(
        self,
        *,
        session_id: str,
        turn_id: str,
        model: str,
        provider: str,
        exc: AdapterError,
        parent_event_id: str | None,
    ) -> None:
        self._bus.emit(
            make_event(
                type="llm.call_failed",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.AGENT,
                payload=LLMCallFailed(
                    model=model,
                    provider=provider,
                    error_class=exc.error_class.value,  # type: ignore[arg-type]
                    error_message_redacted=str(exc),
                    retry_count=0,
                    latency_ms=0,
                ),
                timestamp=_now(),
                parent_event_id=parent_event_id,
            )
        )

    def _emit_turn_completed(
        self,
        *,
        session_id: str,
        turn_id: str,
        stop_reason: StopReason,
        llm_calls: int,
        tool_calls: int,
        input_tokens: int,
        output_tokens: int,
        cost: Decimal,
        wall_time: float,
        parent_event_id: str,
        final_response_text: str | None = None,
        user_prompt_text: str | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        if stop_reason == StopReason.CANCELLED:
            return  # turn.cancelled is its own event type
        # Map adapter stop_reason → catalog enum literal (drop CANCELLED/ERROR).
        catalog_stop = stop_reason.value
        if catalog_stop not in ("end_turn", "max_tokens", "stop_sequence", "tool_use"):
            catalog_stop = "end_turn"
        # Compose signals_extra per evaluator.md §5.1. `final_response_text`
        # feeds the heuristic content-penalty path; `assistant_response_text`
        # is an intentional alias that feeds the LLM judge's
        # `_build_user_message` reader. Both names point at the same string
        # so a single mutation keeps them consistent. Omit any key whose
        # value is empty so absent text is honest (the judge's
        # "(not available)" fallback fires correctly).
        signals_extra: dict | None = None
        extras: dict[str, str] = {}
        if final_response_text:
            extras["final_response_text"] = final_response_text
            extras["assistant_response_text"] = final_response_text
        if user_prompt_text:
            extras["user_prompt_text"] = user_prompt_text
        if extras:
            signals_extra = extras
        self._bus.emit(
            make_event(
                type="turn.completed",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.WORKER if parent_session_id else Actor.AGENT,
                payload=TurnCompleted(
                    stop_reason=catalog_stop,  # type: ignore[arg-type]
                    llm_call_count=llm_calls,
                    tool_call_count=tool_calls,
                    total_input_tokens=input_tokens,
                    total_output_tokens=output_tokens,
                    total_cost_usd=float(cost),
                    wall_time_seconds=wall_time,
                    signals_extra=signals_extra,
                    parent_session_id=parent_session_id,
                ),
                timestamp=_now(),
                parent_event_id=parent_event_id,
            )
        )

    def _emit_turn_cancelled(
        self,
        session_id: str,
        turn_id: str,
        *,
        reason: str,
        partial_llm_calls: int,
        partial_tool_calls: int,
    ) -> None:
        self._bus.emit(
            make_event(
                type="turn.cancelled",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.USER if reason == "user_cancel" else Actor.SYSTEM,
                payload=TurnCancelled(
                    reason=reason,  # type: ignore[arg-type]
                    partial_llm_calls=partial_llm_calls,
                    partial_tool_calls=partial_tool_calls,
                ),
                timestamp=_now(),
            )
        )

    def _emit_memory_events_for_changes(
        self,
        *,
        memory_before: dict,
        memory: MemoryStore | None,
        tool_uses: list,
        session_id: str,
        turn_id: str,
        parent_event_id: str,
    ) -> None:
        """Diff before/after hashes per file and emit memory.updated for
        each change, plus memory.eviction if the file is over its soft cap.

        Operation is inferred from which memory tool ran in this batch.
        """
        if memory is None:
            return
        memory_after = _memory_hashes(memory)
        ops_by_file: dict[str, str] = {}
        for tu in tool_uses:
            name = tu.name
            file_arg = (tu.input or {}).get("file")
            if not file_arg:
                continue
            if name == "memory_add":
                ops_by_file[file_arg] = "add"
            elif name == "memory_replace":
                ops_by_file[file_arg] = "replace"
            elif name == "memory_consolidate":
                ops_by_file[file_arg] = "consolidate"
        for file_name, after in memory_after.items():
            before = memory_before.get(file_name)
            if before is None or before == after:
                continue
            operation = ops_by_file.get(file_name, "consolidate")
            self._bus.emit(
                make_event(
                    type="memory.updated",
                    session_id=session_id,
                    turn_id=turn_id,
                    actor=Actor.AGENT,
                    payload=MemoryUpdated(
                        file=file_name,  # type: ignore[arg-type]
                        operation=operation,  # type: ignore[arg-type]
                        before_hash=before["hash"],
                        after_hash=after["hash"],
                        before_size_bytes=before["size"],
                        after_size_bytes=after["size"],
                    ),
                    timestamp=_now(),
                    parent_event_id=parent_event_id,
                )
            )
            # Over-soft-cap → eviction warning (no auto-truncate).
            from metis_core.memory.store import MemoryFile
            from metis_core.memory.store import MemoryStore as _MS

            mf = MemoryFile(file_name)
            if after["size"] > _MS.soft_cap(mf):
                self._bus.emit(
                    make_event(
                        type="memory.eviction",
                        session_id=session_id,
                        turn_id=turn_id,
                        actor=Actor.SYSTEM,
                        payload=MemoryEviction(
                            file=file_name,  # type: ignore[arg-type]
                            trigger="size_cap_exceeded",
                            entries_evicted=0,
                            size_before_bytes=before["size"],
                            size_after_bytes=after["size"],
                        ),
                        timestamp=_now(),
                        parent_event_id=parent_event_id,
                    )
                )


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _assistant_text(content: list[ContentBlock]) -> str:
    """Concatenate text blocks for CLI display. Ignores tool_use/thinking."""
    return "\n".join(b.text for b in content if isinstance(b, TextBlock))


def _assemble_volatile_memory(memory: MemoryStore) -> str | None:
    """Compose the *volatile* portion of the system prompt — USER.md and
    MEMORY.md, the per-session memory the agent mutates.

    Returned text trails the cache-control breakpoint on Anthropic and
    sits at the end of the system message on OpenAI / OpenRouter (see
    `docs/specs/context-assembler.md` §2-§5). Returns None when both
    files are empty so the adapter can omit the trailing block entirely
    and a memory-less session pays no cache penalty.
    """
    composed = memory.assemble_system_prompt("").strip()
    return composed or None


def _assemble_stable_system_prompt(
    *,
    base_prompt: str,
    skill_store: Any,
    adapter,
    tools,
) -> tuple[str, list]:
    """Compose the stable system prompt: base persona + discovery index
    (with v3 §5.2.2 `[preloaded]` annotation) + v2 §5.1 padding.

    Returns the final prefix plus the list of `Skill` objects whose
    bodies were inlined as padding (i.e. the pre-activated skills the
    caller should record in the activation registry and emit
    `skill.loaded(load_reason="always")` for).

    Composition order:
      1. Render the discovery index without annotation.
      2. Run padding to determine which skills get inlined.
      3. Patch the rendered prefix to add `[preloaded]` annotations on
         the index lines for inlined skills. The patch is a fixed
         string substitution — no re-padding, byte-stable per session.

    Skills inlined as padding land on the *stable* side of the cache
    breakpoint (along with the discovery index), so the bytes are
    cached after the first turn. The `[preloaded]` annotation tells the
    agent the body is already in context.
    """
    prefix = base_prompt
    if skill_store is not None and len(skill_store) > 0:
        prefix = _append_skill_index(prefix, skill_store, preloaded=frozenset())
    padded, inlined = _pad_stable_prefix_for_cache(
        stable_prefix=prefix,
        adapter=adapter,
        tools=tools,
        skill_store=skill_store,
    )
    if inlined:
        preloaded_names = {s.name for s in inlined}
        padded = _annotate_index_for_preloaded(padded, preloaded_names)
    return padded, inlined


def _annotate_index_for_preloaded(prefix: str, preloaded_names: set[str]) -> str:
    """Patch the rendered discovery-index lines for `preloaded_names`
    from `- {name}: {description}` to `- {name} [preloaded]: {description}`.

    Byte-stable: the replacement is a fixed `:` → ` [preloaded]:` insertion
    on the index line. Idempotent against double-patching (the second
    pass finds no match).
    """
    out = prefix
    for name in preloaded_names:
        # Anchor the match on the index-line format so we don't touch
        # other text that happens to start with `- name:`.
        old = f"\n- {name}: "
        new = f"\n- {name} [preloaded]: "
        out = out.replace(old, new, 1)
    return out


def _append_skill_index(
    system_prompt: str,
    skill_store: Any,
    preloaded: frozenset[str] = frozenset(),
) -> str:
    """Append the discovery index (agentskills.io stage 1) to the system prompt.

    One line per skill: `- <name>: <description>`. Bodies are NOT injected —
    the agent calls `skill_load(name)` to activate one.

    Skills whose bodies were inlined into the stable prefix as v2 §5.1
    padding get a `[preloaded]` annotation per context-assembler.md v3
    §5.2.2 — the agent reads this and knows not to call `skill_load`
    for them (the body is already in the system prompt; a `skill_load`
    call returns a pointer, not the body).
    """
    lines = [
        "## Available skills",
        "Use `skill_search(query)` to filter and `skill_load(name)` to read a body.",
        "",
    ]
    for name, description in skill_store.discovery_index():
        if name in preloaded:
            lines.append(f"- {name} [preloaded]: {description}")
        else:
            lines.append(f"- {name}: {description}")
    return system_prompt.rstrip() + "\n\n" + "\n".join(lines)


def _memory_hashes(memory: MemoryStore | None) -> dict:
    """Snapshot the current hash + size of each memory file. Empty/missing
    files have an empty hash and size 0."""
    import hashlib as _hashlib

    if memory is None:
        return {}
    snapshot: dict = {}
    for name in ("MEMORY.md", "USER.md"):
        content = memory.read(name)
        snapshot[name] = {
            "hash": _hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "size": len(content.encode("utf-8")),
        }
    return snapshot


async def _consume_stream(
    stream,
    on_event: StreamHandler | None,
) -> MessageComplete:
    """Iterate an adapter stream, forwarding each event to `on_event` if set,
    and return the MessageComplete event (which carries final state)."""
    final: MessageComplete | None = None
    async for event in stream:
        if on_event is not None:
            result = on_event(event)
            if inspect.isawaitable(result):
                await result
        if isinstance(event, MessageComplete):
            final = event
    if final is None:
        # Stream ended without MessageComplete — synthesize an empty one so
        # the caller has something to work with. This shouldn't happen with
        # a well-behaved adapter.
        final = MessageComplete(
            message_id="",
            stop_reason=StopReason.END_TURN,
            final_content=[],
            usage=TokenUsage(0, 0),
            latency_ms=0,
        )
    return final


_USER_EXPLICIT_POLICIES = ("per_message_override", "manual_sticky")


def _user_explicit_rejection(decision: RoutingDecision) -> UserExplicitModelRejectedError | None:
    """If the user's explicit model choice (per-message override or session
    sticky) was rejected by routing validation, build the error to raise.

    Routing's default behavior is to fall through to the next candidate
    when validation fails. For user-explicit choices that's the wrong UX:
    the user picked a specific model, so we'd rather refuse the turn than
    bill them for something else. Returns None when neither user-explicit
    slot was rejected (either the user's choice won, or no explicit choice
    was set in the first place).
    """
    for evaluation in decision.chain:
        if evaluation.policy not in _USER_EXPLICIT_POLICIES:
            continue
        if evaluation.verdict != "rejected":
            continue
        source = (
            "@model override" if evaluation.policy == "per_message_override" else "active model"
        )
        return UserExplicitModelRejectedError(
            source=source,
            model=evaluation.candidate_model or "",
            validation_failure=evaluation.validation_failure or "rejected",
            would_fall_back_to=decision.chosen_model or None,
        )
    return None


def _mode_for_chain_index(index: int) -> RoutingMode:
    """Map a routing-chain index to the RoutingDecisionRecord.mode summary
    enum (canonical-format §4.3 mapping table)."""
    # chain order: per_message_override, manual_sticky, rule, pattern,
    # delegate_request, workspace_default, global_default
    if index == 0:
        return RoutingMode.OVERRIDE
    if index == 1:
        return RoutingMode.MANUAL
    if index == 2:
        return RoutingMode.RULE
    if index == 3:
        return RoutingMode.PATTERN
    if index == 4:
        return RoutingMode.DELEGATE
    return RoutingMode.DEFAULT


def _heuristic_token_estimate(history: list[Message], system_prompt: str | None) -> int:
    chars = len(system_prompt or "")
    for m in history:
        for block in m.content:
            chars += len(getattr(block, "text", ""))
    return max(1, chars // 4)


class _HeuristicAdapter:
    """Minimal adapter stand-in for `_pad_stable_prefix_for_cache` when no
    real adapter is available at session-init time. Uses the same
    `~chars/4` heuristic that real adapters use for `estimate_input_tokens`
    (provider-adapter-contract.md §3.1), so the padding decision is the
    same whether or not a real adapter is registered."""

    def estimate_input_tokens(
        self,
        messages: list,
        tools: list,
        system_prompt: str | None,
    ) -> int:
        chars = len(system_prompt or "")
        for tool in tools or []:
            chars += len(getattr(tool, "description", "")) + len(
                str(getattr(tool, "input_schema", ""))
            )
        return max(1, chars // 4)


_INTERNAL_WHITESPACE_RUN = re.compile(r" {2,}")
_LEADING_SPACE_RUN = re.compile(r"^( *)")


def _normalize_shared_text(text: str) -> str:
    """Strip alignment whitespace before the shared output crosses into the
    LLM context.

    `/models` and similar slash output use column padding (often 4+ spaces
    between fields) and trailing whitespace from right-padding — useful for
    visual alignment on the human's screen, pure noise to the LLM. Tokenizers
    don't compress mid-line whitespace runs well, so a 30-row /models dump
    can carry 100+ tokens of pure padding.

    Transforms applied per line:

    - Tabs are expanded to 4 spaces (so they don't bias whitespace handling).
    - Trailing whitespace is dropped.
    - Empty lines are dropped.
    - Leading indent is preserved (it carries the tree hierarchy of nested
      provider / namespace headers in `/models` output).
    - Internal runs of 2+ spaces in the line body collapse to a single space.

    The original buffer (what the user saw on screen) is unaffected — only
    the LLM-bound version goes through this. Trace history records the
    normalized text, which is what the agent actually saw.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.expandtabs(4).rstrip()
        if not line:
            continue
        match = _LEADING_SPACE_RUN.match(line)
        lead = match.group(1) if match else ""
        body = line[len(lead) :]
        normalized_body = _INTERNAL_WHITESPACE_RUN.sub(" ", body)
        out.append(lead + normalized_body)
    return "\n".join(out)


def _compose_message_with_shared(shared: str, user_text: str) -> str:
    """Format the user message when `/share` injected slash output.

    Wraps the (normalized) shared block in clear delimiters so the agent
    can see the boundary between "context the user shared from their
    terminal" and "what the user is actually asking." The normalized text
    is what's persisted in history.
    """
    normalized = _normalize_shared_text(shared)
    return (
        "[Shared from my terminal — output of a slash command I ran:]\n"
        f"{normalized}\n"
        "[End of shared output]\n"
        "\n"
        f"{user_text}"
    )


# Re-export RoutingDecision for callers that want the typed result.
__all__ = [
    "OverrideError",
    "SessionManager",
    "TurnResult",
    "UnknownAliasError",
    "UserExplicitModelRejectedError",
]
_ = RoutingDecision  # exported via metis_core.routing
