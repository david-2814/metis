"""Smoke test: prove prompt-cache breakpoints actually pay off.

Drives a 2-turn conversation against the real Anthropic API with a stable
system prompt large enough to sit above Anthropic's caching minimum (1024
tokens on sonnet/opus, 2048 tokens on haiku — we pad above 2048 so any
model in the family caches).

Asserts:
- Turn 1: `cache_creation_input_tokens > 0` (the cache is being written).
- Turn 2: `cached_input_tokens > 0` (the cache is being read).

Cost: < $0.05 per run. Validates `docs/specs/context-assembler.md §3` end
to end against a real provider; the unit tests assert that the wire shape
carries `cache_control`, this test asserts the provider actually honors it.

Usage:
    uv run python scripts/smoke_cache.py [--model haiku|sonnet|opus]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from metis_core.adapters.anthropic import AnthropicAdapter
from metis_core.events.bus import EventBus, EventFilter, Subscription
from metis_core.events.envelope import Event
from metis_core.pricing import DEFAULT_PRICE_TABLE
from metis_core.routing import ModelRegistry, RoutingEngine
from metis_core.sessions import InMemorySessionStore, SessionManager
from metis_core.tools.builtins import register_builtins
from metis_core.tools.dispatcher import ToolDispatcher

ANTHROPIC_MODELS = {
    "anthropic:claude-opus-4-7": ["opus", "deep"],
    "anthropic:claude-sonnet-4-6": ["sonnet", "balanced"],
    "anthropic:claude-haiku-4-5": ["haiku", "fast"],
}

REPO_ROOT = Path(__file__).resolve().parent.parent

# Padding designed to clear Anthropic's per-model cache minimum with
# margin. Haiku's floor is 2048 tokens; sonnet/opus floors are 1024. The
# first iteration of this script targeted ~2050 tokens, which sat right
# at the haiku floor — and Anthropic appears to silently drop cache_control
# when the cached prefix tokenizes below the floor. The padding here
# targets >3000 tokens of stable instructions with margin. Each guideline
# is intentionally distinct text so BPE tokenization can't compress
# repeated lines and undercount.
_GUIDELINE_VARIATIONS = [
    "be precise; cite file paths with line numbers; prefer the smallest correct edit",
    "never invent APIs that aren't in the provided context; surface ambiguity rather than guessing",
    "when modifying code, preserve existing formatting, import order, and naming conventions",
    "before refactoring, prove the test suite passes; after refactoring, prove it still passes",
    "treat shared dependencies as load-bearing; coordinate breaking changes with their consumers",
    "log decisions inline with a short why; reviewers should not have to reconstruct intent",
    "when a function grows past one screen, split it; long functions hide bugs in their middles",
    "validate inputs at the system boundary; trust internal callers within the same module",
    "prefer immutable structures unless the call site demonstrably needs mutation",
    "name things after what they are, not how they are used; usage drifts faster than identity",
    "when something is unclear, write a one-line clarifying question, not a five-paragraph guess",
    "every new file should be small; large files are usually two files pretending to be one",
    "tests that exercise the real database catch migration bugs that mocks silently approve",
    "TODOs without a date or owner are camouflage for abandonment; either fix or file an issue",
    "when error messages are seen by a human, name the action that triggered them, not the layer",
    "when error messages are seen by a machine, keep the shape stable across releases",
    "feature flags decay; remove them within one release of full rollout or full retirement",
    "concurrency bugs hide in shared mutable state; prefer message passing over shared locks",
    "performance work without a measurement is decoration; profile before, profile after",
    "if you find yourself writing a comment to explain a name, rename the thing instead",
]


def _build_padding() -> str:
    lines = ["## Operating context"]
    for i in range(1, 161):
        variant = _GUIDELINE_VARIATIONS[(i - 1) % len(_GUIDELINE_VARIATIONS)]
        lines.append(f"- Guideline {i:03d}: {variant}.")
    lines.append("")
    lines.append("## Style")
    lines.append(
        "Be terse. Lead with the answer. Code blocks only when they're load-bearing. "
        "When citing a file, use the form path/to/file.py:LINE. When making a claim "
        "the user can check, link to the source. When you don't know, say so and "
        "name what would resolve the uncertainty."
    )
    lines.append("")
    lines.append("## Tool use")
    lines.append(
        "Tools are owned by the workspace, not by the conversation. A tool call that "
        "mutates state should be obviously safe to retry; if it isn't, fail loudly rather "
        "than re-run and double-apply. When listing files, prefer the smallest pattern that "
        "answers the question. When reading files, read once into memory and reason there, "
        "rather than re-reading on every step. When writing files, write the whole final "
        "shape, not an in-progress checkpoint. When running shell commands, never assume "
        "the working directory; pass absolute paths or set cwd explicitly."
    )
    return "\n".join(lines) + "\n"


_STABLE_PADDING = _build_padding()


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Metis prompt-cache smoke test.")
    parser.add_argument("--model", default="haiku", help="alias or canonical id")
    args = parser.parse_args()

    _load_dotenv(REPO_ROOT / ".env")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or "sk-ant-..." in api_key:
        print("ANTHROPIC_API_KEY not set (or still the placeholder).", file=sys.stderr)
        return 1

    bus = EventBus()
    bus.start()

    # Capture llm.call_completed events so we can read cached_input_tokens.
    completed: list[Event] = []

    async def collector(e: Event) -> None:
        if e.type == "llm.call_completed":
            completed.append(e)

    bus.subscribe(
        Subscription(filter=EventFilter(), handler=collector, name="cache-smoke", fast_path=True)
    )

    adapter = AnthropicAdapter(api_key=api_key)
    registry = ModelRegistry()
    for model_id, aliases in ANTHROPIC_MODELS.items():
        registry.register(model_id=model_id, adapter=adapter, aliases=aliases)

    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    register_builtins(dispatcher)

    # Use a long, stable system prompt so the breakpoint sits above the
    # provider's caching minimum.
    long_system_prompt = (
        "You are Metis, an AI assistant operating in a developer's workspace. "
        "Use the available tools to read and modify files, run shell commands, "
        "and answer questions about the workspace. Be concise.\n\n" + _STABLE_PADDING
    )

    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
        system_prompt=long_system_prompt,
    )

    resolved = registry.resolve_alias(args.model)
    if resolved is None:
        print(f"unknown model: {args.model}", file=sys.stderr)
        return 1

    session = manager.create_session(workspace_path=str(REPO_ROOT), active_model=resolved)

    print("=== Metis prompt-cache smoke test ===")
    print(f"Model:                 {resolved}")
    print(
        f"System prompt length:  {len(long_system_prompt)} chars (~{len(long_system_prompt) // 4} tok)"
    )
    print()

    turns = [
        "Reply with the single word OK and nothing else.",
        "Now reply with the single word THANKS and nothing else.",
    ]

    exit_code = 0
    for i, prompt in enumerate(turns, 1):
        print(f"--- Turn {i}: {prompt}")
        try:
            result = await manager.submit_turn(session.id, prompt)
        except Exception as exc:
            print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            exit_code = 1
            break
        print(f"  reply: {result.assistant_text!r}")
        print(
            f"  [cost=${result.cost_usd:.6f} "
            f"in={result.input_tokens} out={result.output_tokens} "
            f"wall={result.wall_time_seconds:.2f}s]"
        )
        print()

    await bus.drain()
    await bus.stop()
    await adapter.close()

    print("=== Cache effectiveness ===")
    if len(completed) < 2:
        print(f"FAILED: expected >= 2 llm.call_completed events, got {len(completed)}")
        return 1

    turn1 = completed[0].payload
    turn2 = completed[1].payload
    print(
        f"Turn 1 cache_creation_input_tokens = {turn1['cache_creation_input_tokens']}, "
        f"cached_input_tokens = {turn1['cached_input_tokens']}"
    )
    print(
        f"Turn 2 cache_creation_input_tokens = {turn2['cache_creation_input_tokens']}, "
        f"cached_input_tokens = {turn2['cached_input_tokens']}"
    )

    # Turn 1 should have written to the cache (system prompt is large enough).
    if turn1["cache_creation_input_tokens"] <= 0:
        print(
            "FAILED: turn 1 cache_creation_input_tokens == 0 — system prompt may be "
            "below the provider's caching minimum (1024 tokens on sonnet/opus, "
            "2048 tokens on haiku). Increase _STABLE_PADDING.",
            file=sys.stderr,
        )
        exit_code = 1

    # Turn 2 must read from the cache. This is the load-bearing assertion.
    if turn2["cached_input_tokens"] <= 0:
        print(
            "FAILED: turn 2 cached_input_tokens == 0 — the cache breakpoint isn't "
            "landing where it should. Check adapters/anthropic.py "
            "_tools_to_anthropic_with_cache and _system_blocks.",
            file=sys.stderr,
        )
        exit_code = 1
    else:
        total_cost = sum(float(c.payload["cost_usd"]) for c in completed)
        print(f"PASSED. Total cost across both turns: ${total_cost:.6f}")

    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
