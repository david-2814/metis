"""Smoke test: prove prompt-cache breakpoints actually pay off.

Drives a 2-turn conversation against the real Anthropic API using the
*natural* Metis system prompt (`DEFAULT_SYSTEM_PROMPT` + built-in tools).
The session manager's §5.1 minimum-cacheable-prefix rule appends the
deterministic operating-context padding so the cached prefix clears the
provider's per-model cache floor automatically.

Asserts:
- Turn 1: `cache_creation_input_tokens > 0` (the cache is being written).
- Turn 2: `cached_input_tokens > 0` (the cache is being read).

Cost: < $0.05 per run. Validates `docs/specs/context-assembler.md §3
(breakpoint placement) and §5.1 (minimum-cacheable-prefix padding)
end to end against a real provider. The unit tests assert that the
wire shape carries `cache_control` and that the stable prefix is
padded above the cache floor; this test asserts the provider actually
honors both.

Usage:
    uv run python scripts/smoke_cache.py [--model haiku|sonnet|opus]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from metis.core.adapters.anthropic import AnthropicAdapter
from metis.core.events.bus import EventBus, EventFilter, Subscription
from metis.core.events.envelope import Event
from metis.core.pricing import DEFAULT_PRICE_TABLE
from metis.core.routing import ModelRegistry, RoutingEngine
from metis.core.sessions import InMemorySessionStore, SessionManager
from metis.core.tools.builtins import register_builtins
from metis.core.tools.dispatcher import ToolDispatcher

ANTHROPIC_MODELS = {
    "anthropic:claude-opus-4-7": ["opus", "deep"],
    "anthropic:claude-sonnet-4-6": ["sonnet", "balanced"],
    "anthropic:claude-haiku-4-5": ["haiku", "fast"],
}

REPO_ROOT = Path(__file__).resolve().parent.parent


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

    # Use the *natural* Metis system prompt — no custom padding here.
    # SessionManager's §5.1 minimum-cacheable-prefix rule pads the stable
    # prefix to clear the haiku-4-5 effective cache floor automatically.
    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
    )

    resolved = registry.resolve_alias(args.model)
    if resolved is None:
        print(f"unknown model: {args.model}", file=sys.stderr)
        return 1

    session = manager.create_session(workspace_path=str(REPO_ROOT), active_model=resolved)

    print("=== Metis prompt-cache smoke test ===")
    print(f"Model:                 {resolved}")
    print("Mode:                  natural Metis system prompt (relies on §5.1 padding)")
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

    # Turn 1 should have written to the cache (the §5.1 padding lifts the
    # stable prefix above the provider's effective cache floor).
    if turn1["cache_creation_input_tokens"] <= 0:
        print(
            "FAILED: turn 1 cache_creation_input_tokens == 0 — the §5.1 "
            "minimum-cacheable-prefix padding did not lift the prefix above "
            "the provider's effective cache floor. Check "
            "sessions/manager.py:_pad_stable_prefix_for_cache and "
            "MIN_CACHEABLE_PREFIX_TOKENS.",
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
